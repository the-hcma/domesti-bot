"""Hermetic tests for delayed rule device_actions (issue #485)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import RuleConditionsOut, RuleDeviceActionOut, RuleOut, UsersInsideGeofenceCondition
from app.deferred_device_action_store import list_deferred_device_actions
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.pending_fire_notification_store import (
    insert_pending_fire_notification,
    list_pending_fire_notifications,
)
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleActionDispatchError
from app.rule_device_action_outcome import RuleDeviceActionOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool = True) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.host = host
        self.mac_address = None
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []
        self._on = is_on

    @property
    def is_on(self) -> bool:
        return self._on

    async def turn_on(self) -> None:
        self._on = True
        self.calls.append("on")

    async def turn_off(self) -> None:
        self._on = False
        self.calls.append("off")


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


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    mgr.get_device_by_alias.return_value = None
    return cast(KasaDeviceManager, mgr)


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


def _power_cycle_rule(
    *,
    delay_s: int = 60,
    enabled: bool = True,
    include_immediate: bool = True,
    notify_on_fire: bool = False,
) -> RuleOut:
    actions: list[RuleDeviceActionOut] = []
    if include_immediate:
        actions.append(
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_OFF,
                device_id="HDHomeRun tuner",
                family_id=DeviceFamilyId.KASA,
            ),
        )
    actions.append(
        RuleDeviceActionOut(
            action=RuleDeviceActionType.TURN_ON,
            delay_s=delay_s,
            device_id="HDHomeRun tuner",
            family_id=DeviceFamilyId.KASA,
        ),
    )
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=actions,
        enabled=enabled,
        id="hdhomerun-nightly-power-cycle",
        label="Nightly HDHomeRun tuner power cycle",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"] if notify_on_fire else [],
        notify_on_fire=notify_on_fire,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


async def _await_calls(device: _FakeKasa, expected: list[str]) -> None:
    deadline = asyncio.get_running_loop().time() + 2.0
    while device.calls != expected:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0)
    assert device.calls == expected


async def _await_send_count(send_mock: MagicMock, expected: int) -> None:
    """Wait until the notify mock has been called ``expected`` times.

    Pending-fire rows are deleted *before* ``asyncio.to_thread`` finishes the
    SMTP call, so waiting on an empty pending list races the send mock.
    """
    deadline = asyncio.get_running_loop().time() + 2.0
    while send_mock.call_count < expected:
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(0)
    assert send_mock.call_count == expected


@pytest.mark.asyncio
async def test_immediate_and_delayed_actions_dispatch_in_sequence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator.on_location_update("henrique")
        assert device.calls == ["off"]
        assert len(evaluator._deferred_device_actions) == 1

        clock["now"] += 60.0
        evaluator._deferred_device_actions_wake.set()
        await _await_calls(device, ["off", "on"])
        assert evaluator._deferred_device_actions == []
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_only_delayed_actions_still_stamp_last_fired_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=30, include_immediate=False)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner", is_on=False)
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        fire_at = clock["now"] + 60.0
        clock["now"] = fire_at
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator.on_location_update("henrique")
        assert device.calls == []
        fire_state = evaluator.fire_state_for_rule(rule.id)
        assert fire_state.last_fired_at == fire_at
        assert len(evaluator._deferred_device_actions) == 1

        clock["now"] += 30.0
        evaluator._deferred_device_actions_wake.set()
        await _await_calls(device, ["on"])
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_disable_rule_cancels_pending_delayed_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator.on_location_update("henrique")
        assert device.calls == ["off"]
        assert len(evaluator._deferred_device_actions) == 1

        _write_bundle(bundle, rule.model_copy(update={"enabled": False}))
        evaluator._prune_stale_deferred_device_actions()
        assert evaluator._deferred_device_actions == []

        clock["now"] += 60.0
        evaluator._deferred_device_actions_wake.set()
        await asyncio.sleep(0)
        assert device.calls == ["off"]
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_delayed_actions_survive_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A follow-up delayed action still runs after a restart mid-delay."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device_before = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state_before = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device_before]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator_before = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state_before,
        now_fn=lambda: clock["now"],
    )
    evaluator_before.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator_before.on_location_update("henrique")
        assert device_before.calls == ["off"]
        assert len(list_deferred_device_actions(db)) == 1
    finally:
        await evaluator_before.close()

    # Simulate a restart before the delay elapses: new evaluator, same DB.
    clock["now"] += 120.0
    device_after = _FakeKasa("192.168.1.50", "HDHomeRun tuner", is_on=False)
    state_after = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device_after]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator_after = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state_after,
        now_fn=lambda: clock["now"],
    )
    assert len(evaluator_after._deferred_device_actions) == 1
    evaluator_after.start_periodic_tick()
    try:
        evaluator_after._deferred_device_actions_wake.set()
        await _await_calls(device_after, ["on"])
        assert list_deferred_device_actions(db) == []
    finally:
        await evaluator_after.close()


@pytest.mark.asyncio
async def test_reloaded_delayed_action_waits_for_device_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due reloaded action is kept (not dropped) until discovery finishes."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator_before = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator_before.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator_before.on_location_update("henrique")
        assert len(list_deferred_device_actions(db)) == 1
    finally:
        await evaluator_before.close()

    clock["now"] += 120.0
    device_after = _FakeKasa("192.168.1.50", "HDHomeRun tuner", is_on=False)
    holder: dict[str, DeviceManagersState | None] = {"state": None}
    state_after = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device_after]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator_after = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: holder["state"],
        now_fn=lambda: clock["now"],
    )
    evaluator_after.start_periodic_tick()
    try:
        # Discovery not ready: the due action is kept, not dropped.
        await asyncio.sleep(0)
        assert device_after.calls == []
        assert len(evaluator_after._deferred_device_actions) == 1
        # Discovery completes -> the reloaded action dispatches.
        holder["state"] = state_after
        evaluator_after._deferred_device_actions_wake.set()
        await _await_calls(device_after, ["on"])
        assert list_deferred_device_actions(db) == []
    finally:
        await evaluator_after.close()


@pytest.mark.asyncio
async def test_disable_rule_deletes_persisted_delayed_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await evaluator.on_location_update("henrique")
        assert len(list_deferred_device_actions(db)) == 1

        _write_bundle(bundle, rule.model_copy(update={"enabled": False}))
        evaluator._prune_stale_deferred_device_actions()
        assert evaluator._deferred_device_actions == []
        assert list_deferred_device_actions(db) == []
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_fire_path_does_not_sleep_for_delayed_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=120, include_immediate=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        clock["now"] += 60.0
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=clock["now"],
                reported_at=clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        with patch("app.rule_evaluator.asyncio.sleep") as sleep_mock:
            await evaluator.on_location_update("henrique")
            sleep_mock.assert_not_called()
        assert device.calls == ["off"]
        assert len(evaluator._deferred_device_actions) == 1
        assert evaluator._deferred_device_actions[0].due_at == clock["now"] + 120.0
    finally:
        await evaluator.close()


async def _fire_power_cycle_enter(
    evaluator: RuleEvaluator,
    db: Path,
    clock: dict[str, float],
) -> None:
    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")


@pytest.mark.asyncio
async def test_notify_on_fire_waits_for_delayed_actions_then_sends_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, notify_on_fire=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    send_mock = MagicMock(return_value=None)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            await _fire_power_cycle_enter(evaluator, db, clock)
            assert device.calls == ["off"]
            assert send_mock.call_count == 0
            pending = list_pending_fire_notifications(db)
            assert len(pending) == 1
            assert len(pending[0].outcomes) == 1

            clock["now"] += 60.0
            evaluator._deferred_device_actions_wake.set()
            await _await_calls(device, ["off", "on"])
            await _await_send_count(send_mock, 1)
            kwargs = send_mock.call_args.kwargs
            assert kwargs["sequence_completed"] is True
            assert kwargs["cancelled_remaining"] is False
            assert len(kwargs["device_action_outcomes"]) == 2
            assert list_pending_fire_notifications(db) == []
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_notify_on_fire_immediate_only_still_sends_at_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_OFF,
                device_id="HDHomeRun tuner",
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        enabled=True,
        id="immediate-only",
        label="Immediate only",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.EDGE_TRUE],
    )
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    send_mock = MagicMock(return_value=None)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            await _fire_power_cycle_enter(evaluator, db, clock)
            assert device.calls == ["off"]
            assert send_mock.call_count == 1
            assert send_mock.call_args.kwargs.get("sequence_completed", False) is False
            assert list_pending_fire_notifications(db) == []
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_notify_on_fire_cancel_mid_cycle_sends_partial_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, notify_on_fire=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    send_mock = MagicMock(return_value=None)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    # Skip start_periodic_tick so the deferred loop cannot race the cancel flush.
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            await _fire_power_cycle_enter(evaluator, db, clock)
            assert send_mock.call_count == 0
            assert len(list_pending_fire_notifications(db)) == 1
            assert len(evaluator._deferred_device_actions) == 1

            disabled = _power_cycle_rule(delay_s=60, enabled=False, notify_on_fire=True)
            _write_bundle(bundle, disabled)
            evaluator._prune_stale_deferred_device_actions()
            await evaluator._flush_ready_pending_fire_notifications()
            assert send_mock.call_count == 1
            kwargs = send_mock.call_args.kwargs
            assert kwargs["cancelled_remaining"] is True
            assert kwargs["sequence_completed"] is True
            assert len(kwargs["device_action_outcomes"]) == 1
            assert list_pending_fire_notifications(db) == []
            assert evaluator._deferred_device_actions == []
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_notify_on_fire_smtp_failure_restores_claimed_pending_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, notify_on_fire=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_120.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    insert_pending_fire_notification(
        db,
        fire_at=1_700_000_060.0,
        notification_detail=None,
        outcomes=(
            RuleDeviceActionOutcome(
                action=RuleDeviceActionType.TURN_OFF,
                after_state="off",
                before_state="on",
                completed_at=1_700_000_060.0,
                device_id="HDHomeRun tuner",
                error=None,
                family_id=DeviceFamilyId.KASA,
                probable=False,
                succeeded=True,
            ),
        ),
        rule_id=rule.id,
    )

    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    try:
        with patch(
            "app.rule_evaluator.send_rule_notification_email",
            side_effect=RuleActionDispatchError("SMTP down"),
        ):
            await evaluator._flush_ready_pending_fire_notifications()
        pending = list_pending_fire_notifications(db)
        assert len(pending) == 1
        assert pending[0].rule_id == rule.id
        assert len(pending[0].outcomes) == 1
    finally:
        await evaluator.close()


@pytest.mark.asyncio
async def test_notify_on_fire_orphan_pending_flushes_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rule = _power_cycle_rule(delay_s=60, notify_on_fire=True)
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_120.0}
    _seed_presence_db(db, reported_at=clock["now"])
    device = _FakeKasa("192.168.1.50", "HDHomeRun tuner")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    insert_pending_fire_notification(
        db,
        fire_at=1_700_000_060.0,
        notification_detail=None,
        outcomes=(
            RuleDeviceActionOutcome(
                action=RuleDeviceActionType.TURN_OFF,
                after_state="off",
                before_state="on",
                completed_at=1_700_000_060.0,
                device_id="HDHomeRun tuner",
                error=None,
                family_id=DeviceFamilyId.KASA,
                probable=False,
                succeeded=True,
            ),
            RuleDeviceActionOutcome(
                action=RuleDeviceActionType.TURN_ON,
                after_state="on",
                before_state="off",
                completed_at=1_700_000_120.0,
                device_id="HDHomeRun tuner",
                error=None,
                family_id=DeviceFamilyId.KASA,
                probable=False,
                succeeded=True,
            ),
        ),
        rule_id=rule.id,
    )
    assert list_deferred_device_actions(db) == []
    assert len(list_pending_fire_notifications(db)) == 1

    send_mock = MagicMock(return_value=None)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    evaluator.start_periodic_tick()
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            evaluator._deferred_device_actions_wake.set()
            await _await_send_count(send_mock, 1)
            kwargs = send_mock.call_args.kwargs
            assert kwargs["sequence_completed"] is True
            assert len(kwargs["device_action_outcomes"]) == 2
            assert list_pending_fire_notifications(db) == []
    finally:
        await evaluator.close()
