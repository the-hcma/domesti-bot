"""Hermetic tests for ``ignore_expected_device_changes`` on device_state rules (#694)."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.expected_device_change import (
    expected_device_changes,
    mark_expected_device_change,
)
from app.kasa_device_manager import KasaDeviceManager
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator

_FAN_MAC = "5c:e9:31:60:c5:bd"


class _FakeKasa:
    def __init__(self, identifier: str, label: str, *, is_on: bool = False) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.mac_address = identifier
        self.host = f"{identifier}.local"
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.is_on = True
        self.calls.append("on")

    async def turn_off(self) -> None:
        self.is_on = False
        self.calls.append("off")


@pytest.fixture(autouse=True)
def _clear_expected_marks() -> Iterator[None]:
    expected_device_changes.clear()
    yield
    expected_device_changes.clear()


@pytest.mark.asyncio
async def test_ignore_expected_skips_marked_turn_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _fan_on_alert_rule(ignore_expected=True))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    fan = _FakeKasa(_FAN_MAC, "Master bedroom fan", is_on=True)
    state = _kasa_state(fan)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: 1_700_000_000.0,
    )
    mark_expected_device_change(DeviceFamilyId.KASA, _FAN_MAC)

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.KASA, _FAN_MAC)

    send_mock.assert_not_called()
    assert evaluator.fire_state_for_rule("fan-on-alert").last_fired_at is None


@pytest.mark.asyncio
async def test_ignore_expected_fires_when_unmarked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _fan_on_alert_rule(ignore_expected=True))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    fan = _FakeKasa(_FAN_MAC, "Master bedroom fan", is_on=True)
    state = _kasa_state(fan)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: 1_700_000_000.0,
    )

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.KASA, _FAN_MAC)

    assert send_mock.call_count == 1
    assert evaluator.fire_state_for_rule("fan-on-alert").last_fired_at is not None


@pytest.mark.asyncio
async def test_default_still_fires_when_marked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _fan_on_alert_rule(ignore_expected=False))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    fan = _FakeKasa(_FAN_MAC, "Master bedroom fan", is_on=True)
    state = _kasa_state(fan)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: 1_700_000_000.0,
    )
    mark_expected_device_change(DeviceFamilyId.KASA, _FAN_MAC)

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.KASA, _FAN_MAC)

    assert send_mock.call_count == 1


def _fan_on_alert_rule(*, ignore_expected: bool) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id=_FAN_MAC,
                            display_name="Master bedroom fan",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="fan-on-alert",
        ignore_expected_device_changes=ignore_expected,
        label="Fan on alert",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
    )


def _kasa_state(device: _FakeKasa) -> DeviceManagersState:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = (device,)
    mgr._device_name_to_device = {device.identifier: device, device.preferred_label: device}
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=cast(KasaDeviceManager, mgr),
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _write_bundle(path: Path, rule: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "mac",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
