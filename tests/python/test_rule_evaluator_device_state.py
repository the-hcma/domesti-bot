"""Hermetic tests for device-dwell rule triggers (garage open while away)."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterLocalTimeCondition,
    DevicesAnyInStateForSCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, Ep1ReadingMetric, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleActionDispatchError, RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_EP1_MAC = "28:05:a5:28:c8:48"
_NY = ZoneInfo("America/New_York")


@pytest.mark.asyncio
async def test_device_dwell_does_not_fire_when_door_just_opened_while_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    door.is_open = True
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    send_mock.assert_not_called()
    assert evaluator.fire_state_for_rule("away-garage-open-alert").last_fired_at is None


@pytest.mark.asyncio
async def test_device_dwell_fires_once_per_away_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0
    door.is_open = True

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        assert send_mock.call_count == 1

        door.is_open = False
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        door.is_open = True
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    assert send_mock.call_count == 1


async def test_device_dwell_fires_when_door_open_for_threshold_while_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    door.is_open = True
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        assert send_mock.call_count == 0
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    send_mock.assert_called_once()
    assert evaluator.fire_state_for_rule("away-garage-open-alert").last_fired_at == (clock["now"])


@pytest.mark.asyncio
async def test_ep1_clear_dwell_fires_when_clear_long_enough(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _ep1_clear_dwell_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    sensor = _FakeEp1Sensor(_EP1_MAC, "Office EP1", occupied=True)
    state = _ep1_state(sensor)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )

    sensor.occupancy_state = DeviceConditionState.CLEAR.value
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.EP1, _EP1_MAC)
        assert send_mock.call_count == 0
        clock["now"] += 20.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.EP1,
            _EP1_MAC,
        )

    send_mock.assert_called_once()
    assert evaluator.fire_state_for_rule("evening-ep1-clear-alert").last_fired_at == clock["now"]


@pytest.mark.asyncio
async def test_ep1_clear_dwell_retries_after_local_time_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-window dwell must not debounce the same clear streak past 21:00 (#681)."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _ep1_clear_dwell_rule(after_hhmm="21:00"))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": datetime(2026, 6, 9, 20, 30, tzinfo=_NY).timestamp()}
    _seed_presence_db(db, now=clock["now"])
    sensor = _FakeEp1Sensor(_EP1_MAC, "Office EP1", occupied=True)
    state = _ep1_state(sensor)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )

    sensor.occupancy_state = DeviceConditionState.CLEAR.value
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.EP1, _EP1_MAC)
        clock["now"] += 20.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.EP1,
            _EP1_MAC,
        )
        assert send_mock.call_count == 0

        clock["now"] = datetime(2026, 6, 9, 21, 0, tzinfo=_NY).timestamp()
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.EP1,
            _EP1_MAC,
        )

    send_mock.assert_called_once()
    assert evaluator.fire_state_for_rule("evening-ep1-clear-alert").last_fired_at == clock["now"]


@pytest.mark.asyncio
async def test_ep1_clear_dwell_failed_fire_does_not_retry_every_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _ep1_clear_dwell_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    sensor = _FakeEp1Sensor(_EP1_MAC, "Office EP1", occupied=True)
    state = _ep1_state(sensor)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )

    sensor.occupancy_state = DeviceConditionState.CLEAR.value
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        side_effect=RuleActionDispatchError("smtp down"),
    ) as send_mock:
        await evaluator.on_device_state_change(DeviceFamilyId.EP1, _EP1_MAC)
        clock["now"] += 20.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.EP1,
            _EP1_MAC,
        )
        clock["now"] += 60.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.EP1,
            _EP1_MAC,
        )

    assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_ep1_clear_dwell_restart_honors_fire_once_per_local_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _ep1_clear_dwell_rule(fire_once_per_local_day=True))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": datetime(2026, 6, 9, 21, 30, tzinfo=_NY).timestamp()}
    _seed_presence_db(db, now=clock["now"])
    sensor = _FakeEp1Sensor(_EP1_MAC, "Office EP1", occupied=False)
    state = _ep1_state(sensor)
    first = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await first.on_device_state_change(DeviceFamilyId.EP1, _EP1_MAC)
        clock["now"] += 20.0
        await first._maybe_process_device_dwell_satisfied(DeviceFamilyId.EP1, _EP1_MAC)
        assert send_mock.call_count == 1

        restarted = RuleEvaluator(
            cache_path=db,
            device_state_getter=lambda: state,
            now_fn=lambda: clock["now"],
        )
        clock["now"] += 20.0
        await restarted.on_device_state_change(DeviceFamilyId.EP1, _EP1_MAC)
        await restarted._maybe_process_device_dwell_satisfied(DeviceFamilyId.EP1, _EP1_MAC)

    assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_schedule_device_state_change_coalesces_in_flight_wakes(
    tmp_path: Path,
) -> None:
    """While one eval is in flight, further schedules dirty-requeue instead of stacking."""
    db = tmp_path / "discovery.sqlite"
    db.touch()
    calls: list[tuple[DeviceFamilyId, str]] = []
    release = asyncio.Event()
    entered = asyncio.Event()

    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )

    async def _gated(
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        reading_metric: Ep1ReadingMetric | None = None,
    ) -> None:
        del reading_metric
        calls.append((family_id, device_id))
        entered.set()
        await release.wait()

    evaluator.on_device_state_change = _gated  # type: ignore[method-assign]
    family_id = DeviceFamilyId.EP1
    device_id = "aa:bb:cc:dd:ee:01"
    key = (family_id, device_id, None)
    call_key = (family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    evaluator.schedule_device_state_change(family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    assert len(calls) == 1
    assert key in evaluator._pending_device_state_change_keys
    release.set()
    for _ in range(100):
        if len(calls) >= 2 and key not in evaluator._in_flight_device_state_change_keys:
            break
        await asyncio.sleep(0.01)
    assert calls == [call_key, call_key]
    assert key not in evaluator._pending_device_state_change_keys
    assert key not in evaluator._in_flight_device_state_change_keys


@pytest.mark.asyncio
async def test_schedule_device_state_change_invokes_real_handler(
    tmp_path: Path,
) -> None:
    """schedule_device_state_change reaches the real on_device_state_change (not only mocks)."""
    db = tmp_path / "discovery.sqlite"
    db.touch()
    calls: list[tuple[DeviceFamilyId, str]] = []
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )
    real = evaluator.on_device_state_change

    async def _wrap(
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        reading_metric: Ep1ReadingMetric | None = None,
    ) -> None:
        calls.append((family_id, device_id))
        await real(family_id, device_id, reading_metric=reading_metric)

    evaluator.on_device_state_change = _wrap  # type: ignore[method-assign]
    family_id = DeviceFamilyId.EP1
    device_id = "aa:bb:cc:dd:ee:04"
    key = (family_id, device_id, None)
    call_key = (family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    for _ in range(100):
        if calls and key not in evaluator._in_flight_device_state_change_keys:
            break
        await asyncio.sleep(0.01)
    assert calls[0] == call_key
    assert 1 <= len(calls) <= 2


@pytest.mark.asyncio
async def test_schedule_device_state_change_releases_key_when_handler_raises(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    db.touch()
    calls = 0

    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )

    async def _boom(
        _family_id: DeviceFamilyId,
        _device_id: str,
        *,
        reading_metric: Ep1ReadingMetric | None = None,
    ) -> None:
        del reading_metric
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("eval boom")

    evaluator.on_device_state_change = _boom  # type: ignore[method-assign]
    family_id = DeviceFamilyId.EP1
    device_id = "aa:bb:cc:dd:ee:02"
    key = (family_id, device_id, None)
    evaluator.schedule_device_state_change(family_id, device_id)
    for _ in range(100):
        if calls >= 1 and key not in evaluator._in_flight_device_state_change_keys:
            break
        await asyncio.sleep(0.01)
    assert calls == 1
    assert key not in evaluator._pending_device_state_change_keys
    evaluator.schedule_device_state_change(family_id, device_id)
    for _ in range(100):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert calls == 2


@pytest.mark.asyncio
async def test_schedule_device_state_change_reschedules_after_raise_with_pending_wake(
    tmp_path: Path,
) -> None:
    """A wake during a failing eval is re-scheduled from ``finally`` (no manual re-wake)."""
    db = tmp_path / "discovery.sqlite"
    db.touch()
    calls = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )

    async def _boom_then_ok(
        _family_id: DeviceFamilyId,
        _device_id: str,
        *,
        reading_metric: Ep1ReadingMetric | None = None,
    ) -> None:
        del reading_metric
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
            raise RuntimeError("eval boom")

    evaluator.on_device_state_change = _boom_then_ok  # type: ignore[method-assign]
    family_id = DeviceFamilyId.EP1
    device_id = "aa:bb:cc:dd:ee:03"
    key = (family_id, device_id, None)
    evaluator.schedule_device_state_change(family_id, device_id)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    evaluator.schedule_device_state_change(family_id, device_id)
    release.set()
    for _ in range(100):
        if calls >= 2 and key not in evaluator._in_flight_device_state_change_keys:
            break
        await asyncio.sleep(0.01)
    assert calls == 2
    assert key not in evaluator._pending_device_state_change_keys
    assert key not in evaluator._in_flight_device_state_change_keys


@pytest.mark.asyncio
async def test_schedule_device_state_change_skips_requeue_after_close(
    tmp_path: Path,
) -> None:
    """close() during an in-flight eval must not requeue a pending dirty wake."""
    db = tmp_path / "discovery.sqlite"
    db.touch()
    calls: list[tuple[DeviceFamilyId, str]] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )

    async def _gated(
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        reading_metric: Ep1ReadingMetric | None = None,
    ) -> None:
        del reading_metric
        calls.append((family_id, device_id))
        entered.set()
        await release.wait()

    evaluator.on_device_state_change = _gated  # type: ignore[method-assign]
    family_id = DeviceFamilyId.EP1
    device_id = "aa:bb:cc:dd:ee:05"
    key = (family_id, device_id, None)
    call_key = (family_id, device_id)
    evaluator.schedule_device_state_change(family_id, device_id)
    await asyncio.wait_for(entered.wait(), timeout=1.0)
    evaluator.schedule_device_state_change(family_id, device_id)
    assert key in evaluator._pending_device_state_change_keys
    await evaluator.close()
    release.set()
    for _ in range(100):
        if key not in evaluator._in_flight_device_state_change_keys:
            break
        await asyncio.sleep(0.01)
    assert calls == [call_key]
    assert key not in evaluator._pending_device_state_change_keys
    assert key not in evaluator._in_flight_device_state_change_keys


@pytest.mark.asyncio
class _FakeEp1Sensor:
    def __init__(self, identifier: str, label: str, *, occupied: bool | None) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self.host = "192.0.2.10"
        self.port = 6053
        self.unresponsive = False
        if occupied is True:
            self.occupancy_state = DeviceConditionState.OCCUPIED.value
        elif occupied is False:
            self.occupancy_state = DeviceConditionState.CLEAR.value
        else:
            self.occupancy_state = "unknown"


class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.mac_address = None
        self.door_key = self.identifier
        self.preferred_label = label
        self.is_open = is_open


def _away_garage_rule(*, cooldown_s: int = 0) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique", "kristen"],
                ),
                DevicesAnyInStateForSCondition(
                    type="devices_any_in_state_for_s",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Left",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                    min_duration_s=1200,
                    state=DeviceConditionState.OPEN,
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[],
        enabled=True,
        id="away-garage-open-alert",
        label="Away garage open alert",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _ep1_clear_dwell_rule(
    *,
    after_hhmm: str | None = None,
    fire_once_per_local_day: bool = False,
) -> RuleOut:
    conditions: list[AfterLocalTimeCondition | DevicesAnyInStateForSCondition] = []
    if after_hhmm is not None:
        conditions.append(
            AfterLocalTimeCondition(
                type="after_local_time",
                time_hhmm=after_hhmm,
            ),
        )
    conditions.append(
        DevicesAnyInStateForSCondition(
            type="devices_any_in_state_for_s",
            devices=[
                RuleConditionDeviceRefOut(
                    device_id=_EP1_MAC,
                    display_name="Office EP1",
                    family_id=DeviceFamilyId.EP1,
                ),
            ],
            min_duration_s=20,
            state=DeviceConditionState.CLEAR,
        ),
    )
    return RuleOut(
        conditions=RuleConditionsOut(all=list(conditions)),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=fire_once_per_local_day,
        id="evening-ep1-clear-alert",
        label="EP1 clear dwell alert",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _ep1_state(*sensors: _FakeEp1Sensor) -> DeviceManagersState:
    mgr = MagicMock(spec=Ep1DeviceManager)
    mgr.devices = tuple(sensors)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=mgr,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _seed_presence_db(db: Path, *, now: float) -> None:
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Phone",
                enabled=True,
            ),
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    replace_geofences(
        db,
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
    for user_id in ("henrique", "kristen"):
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id=user_id,
                lat=44.0,
                lon=-73.0,
                accuracy_m=20,
                fix_at=now,
                reported_at=now,
                source="test",
            ),
            retention=default_location_history_retention(),
        )


def _tailwind_state(*doors: _FakeTailwindDoor) -> DeviceManagersState:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=mgr,
        vizio_mgr=None,
    )


def _write_bundle(path: Path, rule: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
