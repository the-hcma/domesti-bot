"""Hermetic tests for ``devices_any_transitioned_to_state`` (#848).

A light switched on by its own presence sensor is only visible to domesti-bot
as a power-state change seen by the polling watcher, so the rule must fire on
the off → on transition (inside its clock window), not on the light being on.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AnyConditionsCondition,
    DevicesAnyTransitionedToStateCondition,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    SettingsLocationOut,
    rule_has_device_transition_condition,
)
from app.astronomical_schedule import uses_astronomical_eligibility_wake
from app.device_display import format_device_display
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    RuleDeviceActionType,
    RuleEvaluationCause,
    RuleTrigger,
)
from app.device_rule_wake import DeviceBoolTransition
from app.domesti_bot_cli import DeviceManagersState
from app.expected_device_change import expected_device_changes, mark_expected_device_change
from app.kasa_device_manager import KasaDeviceManager
from app.local_time_schedule import uses_local_time_window_eligibility_wake
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_conditions import (
    DEVICE_TRANSITION_LABEL,
    DEVICE_TRANSITION_MET_DETAIL,
    DEVICE_TRANSITION_OTHER_DETAIL,
    DEVICE_TRANSITION_WAITING_DETAIL,
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rule_evaluator import RuleEvaluator
from app.server_runtime import DomestiServerRuntime

_LAMP_MAC = "5c:e9:31:60:c5:02"
_NY = ZoneInfo("America/New_York")
_OTHER_MAC = "5c:e9:31:60:c5:09"
_RULE_ID = "basement-stairs-companions"
_STAIRS_LABEL = "Basement bottom of stairs"
_STAIRS_MAC = "5c:e9:31:60:c5:01"


class _FakeKasa:
    def __init__(self, identifier: str, label: str, *, is_on: bool = False) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.mac_address = identifier
        self.host = f"{identifier}.local"
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.is_on = False
        self.calls.append("off")

    async def turn_on(self) -> None:
        self.is_on = True
        self.calls.append("on")


@pytest.fixture(autouse=True)
def _clear_expected_marks() -> Any:
    expected_device_changes.clear()
    yield
    expected_device_changes.clear()


def test_rule_with_transition_and_window_validates() -> None:
    rule = _transition_rule()
    assert rule_has_device_transition_condition(rule)


def test_transition_rule_has_no_window_open_eligibility_wake() -> None:
    rule = _transition_rule()
    assert not uses_local_time_window_eligibility_wake(rule)
    assert not uses_astronomical_eligibility_wake(rule)


def test_transition_nested_under_any_is_rejected() -> None:
    with pytest.raises(ValidationError, match="top-level conditions.all"):
        _transition_rule(
            conditions=[
                AnyConditionsCondition(type="any", conditions=[_transition_condition()]),
            ],
        )


def test_two_transition_conditions_are_rejected() -> None:
    with pytest.raises(ValidationError, match="at most one devices_any_transitioned_to_state"):
        _transition_rule(conditions=[_transition_condition(), _transition_condition()])


@pytest.mark.parametrize(
    ("triggers", "schedule_cron"),
    [
        ([RuleTrigger.DEVICE_STATE, RuleTrigger.SCHEDULED], "0 7 * * *"),
        ([RuleTrigger.DEVICE_STATE, RuleTrigger.DWELL_SATISFIED], None),
        ([RuleTrigger.EDGE_TRUE], None),
    ],
)
def test_transition_requires_device_state_trigger_only(
    triggers: list[RuleTrigger],
    schedule_cron: str | None,
) -> None:
    with pytest.raises(ValidationError, match="only the device_state trigger"):
        _transition_rule(triggers=triggers, schedule_cron=schedule_cron)


def test_status_without_transition_reports_waiting() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(_kasa_state(_stairs(is_on=True)), at=_local(7, 0))
    row = evaluate_rule(rule, ctx).conditions[0]
    assert not row.met
    assert row.label == DEVICE_TRANSITION_LABEL.format(state="on")
    assert row.detail == DEVICE_TRANSITION_WAITING_DETAIL.format(
        devices=format_device_display(_STAIRS_MAC, _STAIRS_LABEL),
        state="on",
    )


def test_off_to_on_transition_meets_condition() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(
        _kasa_state(_stairs(is_on=True)),
        at=_local(7, 0),
        transition=_transition(previous=False, current=True),
    )
    row = evaluate_rule(rule, ctx).conditions[0]
    assert row.met
    assert row.detail == DEVICE_TRANSITION_MET_DETAIL.format(
        current="on",
        device=format_device_display(_STAIRS_MAC, _STAIRS_LABEL),
        previous="off",
    )


def test_on_to_off_transition_does_not_meet_condition() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(
        _kasa_state(_stairs(is_on=False)),
        at=_local(7, 0),
        transition=_transition(previous=True, current=False),
    )
    row = evaluate_rule(rule, ctx).conditions[0]
    assert not row.met
    assert row.detail == DEVICE_TRANSITION_OTHER_DETAIL.format(
        current="off",
        device=format_device_display(_STAIRS_MAC, _STAIRS_LABEL),
        previous="on",
        state="on",
    )


def test_transition_to_unknown_does_not_meet_condition() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(
        _kasa_state(_stairs(is_on=False)),
        at=_local(7, 0),
        transition=_transition(previous=False, current=None),
    )
    assert not evaluate_rule(rule, ctx).conditions[0].met


def test_transition_outside_device_state_pass_does_not_meet_condition() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(
        _kasa_state(_stairs(is_on=True)),
        at=_local(7, 0),
        transition=_transition(previous=False, current=True),
        triggered_by=RuleEvaluationCause.ELIGIBILITY,
    )
    assert not evaluate_rule(rule, ctx).conditions[0].met


def test_transition_of_unlisted_device_does_not_meet_condition() -> None:
    rule = _transition_rule(conditions=[_transition_condition()])
    ctx = _evaluation_context(
        _kasa_state(_stairs(is_on=True), _FakeKasa(_OTHER_MAC, "Garage", is_on=True)),
        at=_local(7, 0),
        transition=_transition(previous=False, current=True, device_id=_OTHER_MAC),
    )
    assert not evaluate_rule(rule, ctx).conditions[0].met


@pytest.mark.asyncio
async def test_off_to_on_inside_window_fires_and_turns_on_companion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stairs = _stairs(is_on=True)
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(stairs, lamp), at=_local(7, 0))

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )

    assert send_mock.call_count == 1
    assert send_mock.call_args.kwargs["fire_source"] == "device_state"
    assert lamp.calls == ["on"]


@pytest.mark.asyncio
async def test_off_to_on_outside_window_does_not_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(_stairs(is_on=True), lamp), at=_local(12, 0))

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )

    send_mock.assert_not_called()
    assert lamp.calls == []


@pytest.mark.asyncio
async def test_first_sample_with_light_already_on_does_not_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(_stairs(is_on=True), lamp), at=_local(7, 0))

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.KASA, _STAIRS_MAC)

    send_mock.assert_not_called()
    assert lamp.calls == []


@pytest.mark.asyncio
async def test_on_to_off_inside_window_does_not_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(_stairs(is_on=False), lamp), at=_local(7, 0))

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=True, current=False),
        )

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sensor_off_on_cycle_inside_window_fires_each_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stairs = _stairs(is_on=True)
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    clock = {"now": _local(7, 0)}
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(stairs, lamp), clock=clock)

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )
        stairs.is_on = False
        clock["now"] = _local(7, 10)
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=True, current=False),
        )
        stairs.is_on = True
        clock["now"] = _local(7, 20)
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )

    assert send_mock.call_count == 2


@pytest.mark.asyncio
async def test_rule_caused_turn_on_is_skipped_when_ignoring_expected_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(_stairs(is_on=True), lamp), at=_local(7, 0))
    mark_expected_device_change(DeviceFamilyId.KASA, _STAIRS_MAC)

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )

    send_mock.assert_not_called()


@pytest.mark.asyncio
async def test_sensor_turn_on_fires_when_ignoring_expected_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(_stairs(is_on=True), lamp), at=_local(7, 0))

    with _patched_email() as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            _STAIRS_MAC,
            transition=_transition(previous=False, current=True),
        )

    assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_watcher_transition_reaches_rule_through_runtime_and_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Polling watcher → wake notifier → runtime → coalesced schedule → rule fire."""
    stairs = _stairs(is_on=False)
    lamp = _FakeKasa(_LAMP_MAC, "Basement lamp")
    evaluator = _evaluator(tmp_path, monkeypatch, _kasa_state(stairs, lamp), at=_local(7, 30))
    runtime = DomestiServerRuntime()
    runtime.rule_evaluator = evaluator
    runtime.schedule_vacation_anomaly_alert = MagicMock()  # type: ignore[method-assign]
    notifier = runtime.build_device_rule_wake_notifier()

    with _patched_email() as send_mock:
        notifier.note_bool_transition(DeviceFamilyId.KASA, _STAIRS_MAC, False)
        await _wait_until_idle(evaluator)
        assert send_mock.call_count == 0
        stairs.is_on = True
        notifier.note_bool_transition(DeviceFamilyId.KASA, _STAIRS_MAC, True)
        await _wait_until_idle(evaluator)

    assert send_mock.call_count == 1
    assert lamp.calls == ["on"]


@pytest.mark.asyncio
async def test_transition_arriving_during_in_flight_eval_is_delivered_next(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    db.touch()
    seen: list[DeviceBoolTransition | None] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: _local(7, 0),
    )

    async def _gated(
        _family_id: DeviceFamilyId,
        _device_id: str,
        *,
        reading_metric: object = None,
        transition: DeviceBoolTransition | None = None,
    ) -> None:
        del reading_metric
        seen.append(transition)
        if len(seen) == 1:
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=5.0)

    evaluator.on_device_state_change = _gated  # type: ignore[method-assign]
    first = _transition(previous=False, current=True)
    second = _transition(previous=True, current=False)
    evaluator.schedule_device_state_change(DeviceFamilyId.KASA, _STAIRS_MAC, transition=first)
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    evaluator.schedule_device_state_change(DeviceFamilyId.KASA, _STAIRS_MAC, transition=second)
    release.set()
    await _wait_until_idle(evaluator)

    assert seen == [first, second]
    assert evaluator._pending_device_bool_transitions == {}


def _evaluation_context(
    state: DeviceManagersState,
    *,
    at: float,
    transition: DeviceBoolTransition | None = None,
    triggered_by: RuleEvaluationCause = RuleEvaluationCause.DEVICE_STATE,
) -> RuleEvaluationContext:
    settings = SettingsLocationOut(
        home_label="Home",
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
    )
    now = datetime.fromtimestamp(at, tz=_NY)
    return RuleEvaluationContext(
        device_state=state,
        device_transition=transition,
        geofences=(),
        now=now,
        roster_user_id_lookup={},
        sun=compute_rules_sun_out(settings, now=now),
        timezone=_NY,
        triggered_by=triggered_by,
        user_display_names={},
        user_locations={},
    )


def _evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: DeviceManagersState,
    *,
    at: float | None = None,
    clock: dict[str, float] | None = None,
) -> RuleEvaluator:
    bundle = tmp_path / "rules.json"
    _write_bundle(bundle, _transition_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    times = clock if clock is not None else {"now": at if at is not None else _local(7, 0)}
    return RuleEvaluator(
        cache_path=tmp_path / "discovery.sqlite",
        device_state_getter=lambda: state,
        now_fn=lambda: times["now"],
    )


def _kasa_state(*devices: _FakeKasa) -> DeviceManagersState:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = devices
    name_to_device: dict[str, _FakeKasa] = {}
    for device in devices:
        name_to_device[device.identifier] = device
        name_to_device[device.preferred_label] = device
    mgr._device_name_to_device = name_to_device
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


def _local(hour: int, minute: int) -> float:
    return datetime(2026, 10, 15, hour, minute, tzinfo=_NY).timestamp()


def _patched_email() -> Any:
    return patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    )


def _stairs(*, is_on: bool) -> _FakeKasa:
    return _FakeKasa(_STAIRS_MAC, _STAIRS_LABEL, is_on=is_on)


def _transition(
    *,
    previous: bool,
    current: bool | None,
    device_id: str = _STAIRS_MAC,
) -> DeviceBoolTransition:
    return DeviceBoolTransition(
        current=current,
        device_id=device_id,
        family_id=DeviceFamilyId.KASA,
        previous=previous,
    )


def _transition_condition() -> DevicesAnyTransitionedToStateCondition:
    return DevicesAnyTransitionedToStateCondition(
        type="devices_any_transitioned_to_state",
        devices=[
            RuleConditionDeviceRefOut(
                device_id=_STAIRS_MAC,
                display_name=_STAIRS_LABEL,
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        state=DeviceConditionState.ON,
    )


def _transition_rule(
    *,
    conditions: list[RuleConditionOut] | None = None,
    schedule_cron: str | None = None,
    triggers: list[RuleTrigger] | None = None,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=conditions
            if conditions is not None
            else [
                _transition_condition(),
                LocalTimeWindowCondition(type="local_time_window", start_hhmm="06:30", end_hhmm="09:30"),
            ],
        ),
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_ON,
                device_id=_LAMP_MAC,
                display_name="Basement lamp",
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        enabled=True,
        id=_RULE_ID,
        ignore_expected_device_changes=True,
        label="Basement stairs companions",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        schedule_cron=schedule_cron,
        triggers=triggers if triggers is not None else [RuleTrigger.DEVICE_STATE],
    )


async def _wait_until_idle(evaluator: RuleEvaluator) -> None:
    for _ in range(500):
        if not evaluator._in_flight_device_state_change_keys and not evaluator._pending_device_state_change_keys:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("device-state evaluation did not settle")


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
