"""Hermetic tests for automation rule device action dispatch."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import RuleConditionsOut, RuleDeviceActionOut, RuleOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.rule_actions import (
    RuleActionDispatchError,
    RuleDeviceActionOutcome,
    RuleDeviceDispatchResult,
    RuleNotificationEmailOutcome,
    dispatch_rule_device_actions,
    resolve_kasa_host_by_label,
    send_rule_notification_email,
)
from app.operator_alerts import operator_alert_store
from app.smtp_service import SmtpDeliveryResult
from app.smtp_store import SmtpConfigRecord
from app.sonos_device_manager import SonosDeviceManager, SonosTransitionUnavailableError
from app.vizio_device_manager import VizioDeviceManager


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool = False) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []
        self._is_on = is_on

    @property
    def is_on(self) -> bool:
        return self._is_on

    async def turn_off(self) -> None:
        self.calls.append("off")
        self._is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self._is_on = True


class _FakeVizioTv:
    def __init__(self, device_id: str, label: str, *, is_on: bool) -> None:
        self.identifier = device_id
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True

    def ui_power_state(self) -> str:
        return "on" if self.is_on else "off"


def _device_state(
    kasa_mgr: KasaDeviceManager | None = None,
    *,
    vizio_mgr: VizioDeviceManager | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=kasa_mgr or _kasa_mgr([]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=vizio_mgr,
        cache_path=None,
        args=argparse.Namespace(),
    )


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    return cast(KasaDeviceManager, mgr)


def _vizio_mgr(tvs: list[_FakeVizioTv]) -> VizioDeviceManager:
    mgr = MagicMock(spec=VizioDeviceManager)
    mgr.tvs = tuple(tvs)
    return cast(VizioDeviceManager, mgr)


def test_resolve_kasa_host_by_label_matches_display_name() -> None:
    mgr = _kasa_mgr([_FakeKasa("192.168.1.10", "Garage")])
    assert resolve_kasa_host_by_label(mgr, "Garage") == "192.168.1.10"


def test_resolve_kasa_host_by_label_matches_host() -> None:
    mgr = _kasa_mgr([_FakeKasa("192.168.1.10", "Garage")])
    assert resolve_kasa_host_by_label(mgr, "192.168.1.10") == "192.168.1.10"


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_turns_off_vizio_by_label() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    state = _device_state(vizio_mgr=_vizio_mgr([tv]))
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.VIZIO,
                device_id="Kitchen TV",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
    )
    assert result.errors == ()
    assert len(result.action_outcomes) == 1
    outcome = result.action_outcomes[0]
    assert outcome.before_state == "on"
    assert outcome.after_state == "off"
    assert tv.calls == ["off"]


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_turns_on_by_label() -> None:
    device = _FakeKasa("192.168.1.20", "Front door lights", is_on=False)
    state = _device_state(_kasa_mgr([device]))
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Front door lights",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
    )
    assert result.errors == ()
    assert len(result.action_outcomes) == 1
    outcome = result.action_outcomes[0]
    assert outcome.before_state == "off"
    assert outcome.after_state == "on"
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_collects_unknown_device_error() -> None:
    state = _device_state(_kasa_mgr([]))
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Missing plug",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
    )
    assert len(result.errors) == 1
    assert "Unknown Kasa device" in result.errors[0]


class _FakeSonosZone:
    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        is_playing: bool | None = True,
    ) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_playing = is_playing

    async def pause(self) -> None:
        raise SonosTransitionUnavailableError(
            "Sonos zone 'Living Room' cannot pause from its current transport state "
            "(likely already paused / stopped)."
        )


def _sonos_mgr(zones: list[_FakeSonosZone]) -> SonosDeviceManager:
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = tuple(zones)
    return cast(SonosDeviceManager, mgr)


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_records_probable_sonos_pause_failure() -> None:
    kasa = _FakeKasa("192.168.1.20", "Kitchen lamp")
    sonos = _FakeSonosZone("RINCON_TEST", "Living Room")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([kasa]),
        sonos_mgr=_sonos_mgr([sonos]),
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Kitchen lamp",
                action=RuleDeviceActionType.TURN_OFF,
            ),
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.SONOS,
                device_id="Living Room",
                action=RuleDeviceActionType.PAUSE,
            ),
        ],
    )
    assert kasa.calls == ["off"]
    assert result.errors == ()
    assert len(result.probable_successes) == 1
    assert "Living Room" in result.probable_successes[0]
    assert "(probable)" in result.probable_successes[0]
    sonos_outcome = result.action_outcomes[1]
    assert sonos_outcome.probable is True
    assert sonos_outcome.before_state == "playing"
    assert sonos_outcome.after_state == "paused"


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_probable_failure_uses_expected_state_when_cache_stale() -> None:
    kasa = _FakeKasa("192.168.1.20", "Kitchen lamp", is_on=True)

    async def turn_off_raises_connection_error() -> None:
        raise OSError("connection reset by peer")

    kasa.turn_off = turn_off_raises_connection_error  # type: ignore[method-assign]
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([kasa]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Kitchen lamp",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
    )
    assert result.errors == ()
    assert len(result.probable_successes) == 1
    outcome = result.action_outcomes[0]
    assert outcome.probable is True
    assert outcome.before_state == "on"
    assert outcome.after_state == "off"


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_programmer_error_on_turn_off_is_hard_failure() -> None:
    kasa = _FakeKasa("192.168.1.20", "Kitchen lamp")

    async def broken_turn_off() -> None:
        raise TypeError("unexpected programmer error")

    kasa.turn_off = broken_turn_off  # type: ignore[method-assign]
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([kasa]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )
    result = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Kitchen lamp",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
    )
    assert result.probable_successes == ()
    assert len(result.errors) == 1
    assert "unexpected programmer error" in result.errors[0]
    assert len(result.action_outcomes) == 1
    assert result.action_outcomes[0].succeeded is False
    assert result.action_outcomes[0].probable is False


def test_resolve_kasa_host_by_label_raises_on_ambiguous_label() -> None:
    mgr = _kasa_mgr(
        [
            _FakeKasa("192.168.1.10", "Garage"),
            _FakeKasa("192.168.1.11", "Garage"),
        ]
    )
    with pytest.raises(RuleActionDispatchError, match="Ambiguous Kasa device"):
        resolve_kasa_host_by_label(mgr, "Garage")


def test_send_rule_notification_email_logs_error_when_recipient_missing(
    tmp_path: Path,
) -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=True,
        trigger="edge_true",
    )
    with (
        patch("app.rule_actions._LOGGER.error") as error_mock,
        pytest.raises(RuleActionDispatchError, match="notification_emails"),
    ):
        send_rule_notification_email(tmp_path / "cache.sqlite", rule=rule)
    error_mock.assert_called_once()
    assert "test-rule" in error_mock.call_args.args[1]


def test_send_rule_notification_email_returns_disabled_outcome_when_notify_off(
    tmp_path: Path,
) -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        trigger="edge_true",
    )
    outcome = send_rule_notification_email(tmp_path / "cache.sqlite", rule=rule)
    assert outcome == RuleNotificationEmailOutcome.disabled()
    assert outcome.format_for_log() == "disabled"


def test_send_rule_notification_email_clears_operator_alert_on_success(
    tmp_path: Path,
) -> None:
    operator_alert_store.record_smtp_notification_failure(message="stale failure")
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        trigger="edge_true",
    )
    smtp_config = SmtpConfigRecord(
        from_address="bot@example.com",
        host="smtp.example.com",
        last_test_recipient=None,
        mail_domain="example.com",
        password_configured=False,
        port=25,
        username="",
    )
    delivery = SmtpDeliveryResult(
        host="smtp.example.com",
        port=25,
        recipients=("ops@example.com",),
        smtp_code=250,
        smtp_response="2.0.0 Ok: queued as UNITTEST",
    )
    with (
        patch("app.rule_actions.load_smtp_config", return_value=smtp_config),
        patch("app.rule_actions.smtp_send_ready", return_value=True),
        patch("app.rule_actions.resolve_password_for_send", return_value=""),
        patch("app.smtp_service.deliver_email_message", return_value=delivery),
    ):
        send_rule_notification_email(tmp_path / "cache.sqlite", rule=rule)

    assert operator_alert_store.current_smtp_notification_failure() is None


def test_send_rule_notification_email_records_operator_alert_on_smtp_failure(
    tmp_path: Path,
) -> None:
    operator_alert_store.clear_smtp_notification_failure()
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        trigger="edge_true",
    )
    smtp_config = SmtpConfigRecord(
        from_address="bot@example.com",
        host="smtp.example.com",
        last_test_recipient=None,
        mail_domain="example.com",
        password_configured=False,
        port=25,
        username="",
    )
    with (
        patch("app.rule_actions.load_smtp_config", return_value=smtp_config),
        patch("app.rule_actions.smtp_send_ready", return_value=True),
        patch("app.rule_actions.resolve_password_for_send", return_value=""),
        patch(
            "app.smtp_service.deliver_email_message",
            side_effect=ConnectionRefusedError("connection refused"),
        ),
        pytest.raises(RuleActionDispatchError),
    ):
        send_rule_notification_email(tmp_path / "cache.sqlite", rule=rule)

    alert = operator_alert_store.current_smtp_notification_failure()
    assert alert is not None
    assert alert.reason_code == "smtp_delivery_failed"
    assert "smtp.example.com" in alert.message


def test_send_rule_notification_email_sends_to_all_recipients(
    tmp_path: Path,
) -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com", "alerts@example.com"],
        notify_on_fire=True,
        trigger="edge_true",
    )
    smtp_config = SmtpConfigRecord(
        from_address="bot@example.com",
        host="smtp.example.com",
        last_test_recipient=None,
        mail_domain="example.com",
        password_configured=False,
        port=25,
        username="",
    )
    delivery = SmtpDeliveryResult(
        host="smtp.example.com",
        port=25,
        recipients=("ops@example.com", "alerts@example.com"),
        smtp_code=250,
        smtp_response="2.0.0 Ok: queued as UNITTEST",
    )
    with (
        patch("app.rule_actions.load_smtp_config", return_value=smtp_config),
        patch("app.rule_actions.smtp_send_ready", return_value=True),
        patch("app.rule_actions.resolve_password_for_send", return_value=""),
        patch("app.smtp_service.deliver_email_message", return_value=delivery) as deliver_mock,
    ):
        outcome = send_rule_notification_email(tmp_path / "cache.sqlite", rule=rule)

    assert outcome == RuleNotificationEmailOutcome.sent_to(
        ["ops@example.com", "alerts@example.com"],
        delivery=delivery,
    )
    assert "queue_id=UNITTEST" in outcome.format_for_log()
    message = deliver_mock.call_args[0][1]
    assert message["To"] == "ops@example.com, alerts@example.com"


def test_send_rule_notification_email_includes_device_states_and_rule_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="test-rule",
        label="Test",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        trigger="edge_true",
    )
    smtp_config = SmtpConfigRecord(
        from_address="bot@example.com",
        host="smtp.example.com",
        last_test_recipient=None,
        mail_domain="example.com",
        password_configured=False,
        port=25,
        username="",
    )
    delivery = SmtpDeliveryResult(
        host="smtp.example.com",
        port=25,
        recipients=("ops@example.com",),
        smtp_code=250,
        smtp_response="2.0.0 Ok: queued as UNITTEST",
    )
    outcomes = (
        RuleDeviceActionOutcome(
            action=RuleDeviceActionType.TURN_OFF,
            after_state="off",
            before_state="on",
            device_id="Garage",
            error=None,
            family_id=DeviceFamilyId.KASA,
            probable=False,
            succeeded=True,
        ),
    )
    with (
        patch("app.rule_actions.load_smtp_config", return_value=smtp_config),
        patch("app.rule_actions.smtp_send_ready", return_value=True),
        patch("app.rule_actions.resolve_password_for_send", return_value=""),
        patch("app.smtp_service.deliver_email_message", return_value=delivery) as deliver_mock,
    ):
        send_rule_notification_email(
            tmp_path / "cache.sqlite",
            rule=rule,
            device_action_outcomes=outcomes,
            notification_detail="Garage door is open.",
        )

    message = deliver_mock.call_args[0][1]
    plain_part = message.get_body(preferencelist=("plain",))
    assert plain_part is not None
    plain = plain_part.get_content()
    assert isinstance(plain, str)
    assert "Garage (Kasa): on → off" in plain
    assert "Garage door is open." in plain
    assert (
        "https://domesti.example.com/#/automations/status/test-rule" in plain
    )
