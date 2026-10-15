"""A rule held off by device_not_manually_off_within tells its recipients why (domesti-bot#838)."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterLocalTimeCondition,
    DeviceNotManuallyOffWithinCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    SettingsLocationOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.manual_device_off_store import record_manual_device_off
from app.outbound_email import rule_held_off_provenance_footer
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleActionDispatchError, RuleNotificationEmailOutcome, send_rule_held_off_email
from app.rule_conditions import (
    ManualOffHold,
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
    manual_off_holds,
)
from app.rule_evaluator import RULE_SKIP_REASON_MANUAL_OFF_HOLD, RuleEvaluator
from app.rule_notification import (
    RULE_HELD_OFF_HEADING,
    RULE_HELD_OFF_HOLD_TEMPLATE,
    RULE_HELD_OFF_INTRO_TEMPLATE,
    RULE_HELD_OFF_NEXT_NOTE,
    RULE_HELD_OFF_TRIGGER_TEMPLATE,
    build_rule_held_off_bodies,
    format_completed_at_local,
)
from app.rule_validation import build_roster_user_id_lookup
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users
from app.smtp_service import SmtpConnectionParams, SmtpDeliveryResult

_MAC = "02:00:00:00:00:08"
_NOW = datetime(2026, 10, 14, 19, 0, tzinfo=ZoneInfo("America/New_York"))
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_WINDOW_S = 3600


class _FakeKasa:
    def __init__(self) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = "192.0.2.8"
        self.host = "192.0.2.8"
        self.mac_address = _MAC
        self.identifier = _MAC
        self.preferred_label = "Master bedroom light"
        self.calls: list[str] = []
        self._on = False

    @property
    def is_on(self) -> bool:
        return self._on

    async def turn_on(self) -> None:
        self._on = True
        self.calls.append("on")

    async def turn_off(self) -> None:
        self._on = False
        self.calls.append("off")


def test_held_off_bodies_state_the_facts_and_carry_the_suppressed_footer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    rule = _rule()
    hold = ManualOffHold(
        detail="Master bedroom light (02:00:00:00:00:08) was turned off by hand 22 min ago; waiting 38 min more",
        device_label="Master bedroom light (02:00:00:00:00:08)",
        off_at=_NOW.timestamp() - 22 * 60,
        window_label="60 min",
    )
    plain, html = build_rule_held_off_bodies(
        rule,
        cache_path=None,
        holds=(hold,),
        trigger="its schedule",
    )
    assert RULE_HELD_OFF_INTRO_TEMPLATE.format(label=rule.label, rule_id=rule.id) in plain
    assert RULE_HELD_OFF_TRIGGER_TEMPLATE.format(trigger="its schedule") in plain
    assert RULE_HELD_OFF_HEADING in plain
    assert (
        RULE_HELD_OFF_HOLD_TEMPLATE.format(
            detail=hold.detail,
            when=format_completed_at_local(hold.off_at),
            window=hold.window_label,
        )
        in plain
    )
    assert RULE_HELD_OFF_NEXT_NOTE in plain
    footer = rule_held_off_provenance_footer(rule.id)
    assert footer == "Sent by: domesti-bot · Rule master-bedroom-light-on (suppressed)"
    assert footer in plain
    assert footer in html


def test_held_off_bodies_link_to_the_rule_status_when_a_base_url_is_known(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    hold = ManualOffHold(detail="x", device_label="y", off_at=_NOW.timestamp(), window_label="60 min")
    plain, html = build_rule_held_off_bodies(
        _rule(),
        cache_path=tmp_path / "discovery.sqlite",
        holds=(hold,),
        trigger="its schedule",
    )
    assert "https://domesti.example.com/#/automations/status/master-bedroom-light-on" in plain
    assert 'href="https://domesti.example.com/#/automations/status/master-bedroom-light-on"' in html
    assert "Instance: https://domesti.example.com" in plain


def test_send_rule_held_off_email_mails_the_rule_recipients_with_a_held_off_subject(tmp_path: Path) -> None:
    rule = _rule()
    hold = ManualOffHold(detail="x", device_label="y", off_at=_NOW.timestamp(), window_label="60 min")
    delivery = SmtpDeliveryResult(
        host="smtp.example.com",
        port=25,
        recipients=("ops@example.com",),
        smtp_code=250,
        smtp_response="2.0.0 Ok: queued as UNITTEST",
    )
    params = SmtpConnectionParams(
        from_address="bot@example.com",
        host="smtp.example.com",
        mail_domain="example.com",
        password="",
        port=25,
        username="",
    )
    with (
        patch("app.rule_actions.load_outbound_smtp_params", return_value=params),
        patch("app.outbound_email.deliver_email_message", return_value=delivery) as deliver_mock,
    ):
        outcome = send_rule_held_off_email(tmp_path / "cache.sqlite", holds=(hold,), rule=rule, trigger="its schedule")
    assert outcome == RuleNotificationEmailOutcome.sent_to(["ops@example.com"], delivery=delivery)
    message = deliver_mock.call_args[0][1]
    assert message["To"] == "ops@example.com"
    assert message["Subject"] == f"domesti-bot rule held off: {rule.label}"
    body = message.get_body(preferencelist=("plain",)).get_content()
    assert rule_held_off_provenance_footer(rule.id) in body


def test_send_rule_held_off_email_does_nothing_without_notify_on_fire(tmp_path: Path) -> None:
    rule = _rule().model_copy(update={"notify_on_fire": False, "notification_emails": []})
    hold = ManualOffHold(detail="x", device_label="y", off_at=_NOW.timestamp(), window_label="60 min")
    outcome = send_rule_held_off_email(tmp_path / "cache.sqlite", holds=(hold,), rule=rule, trigger="its schedule")
    assert outcome == RuleNotificationEmailOutcome.disabled()


def test_manual_off_holds_reports_the_hold_when_it_is_the_only_blocker() -> None:
    ctx = _ctx(off_ago_s=22 * 60)
    holds = manual_off_holds(_rule(), evaluate_rule(_rule(), ctx), ctx)
    assert len(holds) == 1
    assert holds[0].off_at == _NOW.timestamp() - 22 * 60
    assert holds[0].device_label == "Master bedroom light (02:00:00:00:00:08)"
    assert holds[0].window_label == "60 min"
    assert "was turned off by hand 22 min ago" in holds[0].detail


def test_manual_off_holds_is_empty_when_the_window_has_elapsed() -> None:
    ctx = _ctx(off_ago_s=_WINDOW_S + 60)
    assert manual_off_holds(_rule(), evaluate_rule(_rule(), ctx), ctx) == ()


def test_manual_off_holds_is_empty_when_another_condition_is_also_unmet() -> None:
    rule = _rule(
        extra=UsersInsideGeofenceCondition(
            type="users_inside_geofence",
            geofence_id="house",
            user_ids=["henrique"],
        ),
    )
    ctx = _ctx(off_ago_s=60)
    evaluation = evaluate_rule(rule, ctx)
    assert [row.met for row in evaluation.conditions] == [False, False]
    assert manual_off_holds(rule, evaluation, ctx) == ()


def test_manual_off_holds_is_empty_when_the_device_reference_does_not_resolve() -> None:
    rule = _rule(device_id="02:00:00:00:00:77")
    ctx = _ctx(off_ago_s=60)
    evaluation = evaluate_rule(rule, ctx)
    assert evaluation.all_met is False
    assert manual_off_holds(rule, evaluation, ctx) == ()


@pytest.mark.asyncio
async def test_edge_rule_held_off_sends_one_notice_per_manual_off_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule())
    send_mock = MagicMock(return_value=None)
    caplog.set_level(logging.INFO, logger="app.rule_evaluator")
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            assert rig.device.calls == []
            assert send_mock.call_count == 1
            kwargs = send_mock.call_args.kwargs
            assert kwargs["rule"].id == "master-bedroom-light-on"
            assert kwargs["trigger"] == "a person arriving or leaving (henrique)"
            assert len(kwargs["holds"]) == 1
            skipped = [r.getMessage() for r in caplog.records if "[rules] skipped" in r.getMessage()]
            assert any(
                f"reason={RULE_SKIP_REASON_MANUAL_OFF_HOLD}" in line and "turned off by hand" in line
                for line in skipped
            )

            await rig.leave()
            await rig.enter()
            assert send_mock.call_count == 1

            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] + 1.0)
            await rig.leave()
            await rig.enter()
            assert send_mock.call_count == 2
            assert rig.device.calls == []
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_edge_rule_fires_without_a_notice_once_the_hold_has_lapsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule())
    send_mock = MagicMock(return_value=None)
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - _WINDOW_S - 600.0)
            await rig.enter()
            assert rig.device.calls == ["on"]
            assert send_mock.call_count == 0
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_no_notice_when_the_hold_is_not_the_only_unmet_condition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = _edge_rule(extra=AfterLocalTimeCondition(type="after_local_time", time_hhmm="23:59"))
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    send_mock = MagicMock(return_value=None)
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            assert rig.device.calls == []
            assert send_mock.call_count == 0
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_no_notice_when_the_rule_does_not_notify_on_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule(notify=False))
    send_mock = MagicMock(return_value=None)
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            assert rig.device.calls == []
            assert send_mock.call_count == 0
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_failed_notice_is_retried_on_the_next_trigger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule())
    send_mock = MagicMock(side_effect=[RuleActionDispatchError("SMTP is not configured"), None])
    # Each leave() advances the clock past the retry cooldown.
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            assert send_mock.call_count == 1
            await rig.leave()
            await rig.enter()
            assert send_mock.call_count == 2
            await rig.leave()
            await rig.enter()
            assert send_mock.call_count == 2
    finally:
        await rig.evaluator.close()


class _Rig:
    """One evaluator wired to a fake Kasa light and a house geofence."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rule: RuleOut) -> None:
        bundle = tmp_path / "rules.json"
        self.db = tmp_path / "discovery.sqlite"
        _write_bundle(bundle, rule)
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
        self.clock = {"now": 1_700_000_000.0}
        _seed_presence_db(self.db, reported_at=self.clock["now"])
        self.device = _FakeKasa()
        kasa_mgr = MagicMock(spec=KasaDeviceManager)
        kasa_mgr.switches = (self.device,)
        kasa_mgr.get_device_by_alias.return_value = None
        state = DeviceManagersState(
            kasa_mgr=cast(KasaDeviceManager, kasa_mgr),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=self.db,
            args=argparse.Namespace(),
        )
        self.evaluator = RuleEvaluator(
            cache_path=self.db,
            device_state_getter=lambda: state,
            now_fn=lambda: self.clock["now"],
        )
        self.evaluator.start_periodic_tick()

    async def enter(self) -> None:
        await self._move(lat=41.194085, lon=-73.888365)

    async def leave(self) -> None:
        await self._move(lat=44.0, lon=-73.0)
        # Stay out past the dwell that debounces the next enter.
        self.clock["now"] += 400.0

    async def _move(self, *, lat: float, lon: float) -> None:
        self.clock["now"] += 60.0
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id="henrique",
                lat=lat,
                lon=lon,
                accuracy_m=20,
                fix_at=self.clock["now"],
                reported_at=self.clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await self.evaluator.on_location_update("henrique")


def _ctx(*, off_ago_s: float) -> RuleEvaluationContext:
    user_display_names = {"henrique": "Henrique"}
    light = MagicMock()
    light.identifier = _MAC
    light.mac_address = _MAC
    light.host = "192.0.2.8"
    light.preferred_label = "Master bedroom light"
    kasa_mgr = MagicMock(spec=KasaDeviceManager)
    kasa_mgr.switches = (light,)
    kasa_mgr.get_device_by_alias.return_value = None
    state = DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        ep1_mgr=None,
        kasa_mgr=kasa_mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )
    return RuleEvaluationContext(
        geofences=(),
        now=_NOW,
        roster_user_id_lookup=build_roster_user_id_lookup(list(user_display_names)),
        sun=compute_rules_sun_out(_SETTINGS, now=_NOW),
        timezone=ZoneInfo("America/New_York"),
        user_display_names=user_display_names,
        user_locations={},
        device_manual_off_at={(DeviceFamilyId.KASA, _MAC): _NOW.timestamp() - off_ago_s},
        device_state=state,
    )


def _edge_rule(
    *,
    extra: AfterLocalTimeCondition | None = None,
    notify: bool = True,
) -> RuleOut:
    conditions: list[object] = [
        UsersInsideGeofenceCondition(
            type="users_inside_geofence",
            geofence_id="house",
            user_ids=["henrique"],
        ),
        _hold_condition(),
    ]
    if extra is not None:
        conditions.append(extra)
    return RuleOut(
        conditions=RuleConditionsOut(all=conditions),  # pyright: ignore[reportArgumentType]
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_ON,
                device_id=_MAC,
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        enabled=True,
        id="master-bedroom-light-on",
        label="Master bedroom light on",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"] if notify else [],
        notify_on_fire=notify,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def _hold_condition(device_id: str = _MAC) -> DeviceNotManuallyOffWithinCondition:
    return DeviceNotManuallyOffWithinCondition(
        type="device_not_manually_off_within",
        device=RuleConditionDeviceRefOut(
            device_id=device_id,
            display_name="Master bedroom light",
            family_id=DeviceFamilyId.KASA,
        ),
        window_s=_WINDOW_S,
    )


def _rule(
    *,
    device_id: str = _MAC,
    extra: UsersInsideGeofenceCondition | None = None,
) -> RuleOut:
    conditions: list[object] = [_hold_condition(device_id)]
    if extra is not None:
        conditions.append(extra)
    return RuleOut(
        conditions=RuleConditionsOut(all=conditions),  # pyright: ignore[reportArgumentType]
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="master-bedroom-light-on",
        label="Master bedroom light on",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        schedule_cron="0 19 * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )


def _seed_presence_db(cache_path: Path, *, reported_at: float) -> None:
    replace_users(
        cache_path,
        [
            UserRecord(
                user_id="henrique",
                first_name="Test",
                last_name="",
                display_name="Test",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    replace_geofences(
        cache_path,
        [
            GeofenceRecord(
                geofence_id="house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.888325,
                radius_m=250,
                enabled=True,
                owntracks_rid=None,
            ),
        ],
    )
    upsert_user_location(
        cache_path,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=reported_at - 400.0,
            reported_at=reported_at - 400.0,
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _write_bundle(path: Path, *rules: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_unexpected_error_while_sending_a_notice_does_not_break_evaluation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule())
    send_mock = MagicMock(side_effect=ValueError("boom"))
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            assert send_mock.call_count == 1
            assert rig.device.calls == []
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_failed_notice_is_not_retried_inside_the_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_edge_rule())
    send_mock = MagicMock(side_effect=RuleActionDispatchError("SMTP is not configured"))
    try:
        with patch("app.rule_evaluator.send_rule_held_off_email", send_mock):
            record_manual_device_off(rig.db, DeviceFamilyId.KASA, _MAC, now=rig.clock["now"] - 600.0)
            await rig.enter()
            await rig.evaluator._notify_rule_held_off(
                _edge_rule(),
                fire_source="immediate",
                holds=(ManualOffHold(detail="x", device_label="y", off_at=1.0, window_label="60 min"),),
                log_user_ids="henrique",
            )
            assert send_mock.call_count == 1
    finally:
        await rig.evaluator.close()
