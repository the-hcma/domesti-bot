"""Hermetic tests for the asyncio rule evaluator."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleConditionDeviceRefOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
    DevicesAnyOnCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import (
    RuleActionDispatchError,
    RuleDeviceDispatchResult,
    RuleNotificationEmailOutcome,
)
from app.rule_evaluator import (
    GeofenceTransition,
    RuleEvaluator,
    _user_triggered_geofence_edge,
)
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool = True) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
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


@dataclass
class _ArriveHomeFixture:
    clock: dict[str, float]
    db: Path
    device: _FakeKasa
    evaluator: RuleEvaluator


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
    return cast(KasaDeviceManager, mgr)


def _seed_presence_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    reported_at: float,
    fix_at: float | None = None,
) -> None:
    fix_epoch = reported_at if fix_at is None else fix_at
    replace_users(
        cache_path,
        [
            UserRecord(
                user_id=user_id,
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
            user_id=user_id,
            lat=lat,
            lon=lon,
            accuracy_m=20,
            fix_at=fix_epoch, reported_at=reported_at,
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _henrique_inside_house_condition() -> UsersInsideGeofenceCondition:
    return UsersInsideGeofenceCondition(
        type="users_inside_geofence",
        geofence_id="house",
        user_ids=["henrique"],
    )


def _arrive_home_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[_henrique_inside_house_condition()]),
        cooldown_s=cooldown_s,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        id="arrive-home",
        label="Arrive home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def _dwell_home_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[],
        enabled=True,
        id="dwell-home",
        label="Dwell home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )


def _setup_arrive_home_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_s: int,
    device_state_getter: Callable[[], DeviceManagersState | None] | None = None,
) -> _ArriveHomeFixture:
    """Create an arrive-home rule evaluator with henrique starting outside the house."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule(cooldown_s=cooldown_s))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0, reported_at=clock["now"] - 400.0,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
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
        device_state_getter=device_state_getter or (lambda: state),
        now_fn=lambda: clock["now"],
    )
    return _ArriveHomeFixture(
        clock=clock,
        db=db,
        device=device,
        evaluator=evaluator,
    )


def _setup_dwell_home_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_s: int,
) -> _ArriveHomeFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _dwell_home_rule(cooldown_s=cooldown_s))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"], reported_at=clock["now"],
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    return _ArriveHomeFixture(
        clock=clock,
        db=db,
        device=_FakeKasa("192.168.1.10", "Garage"),
        evaluator=evaluator,
    )


def _move_henrique_inside_house(fixture: _ArriveHomeFixture) -> None:
    fixture.clock["now"] += 60.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )


@pytest.mark.asyncio
async def test_rule_evaluator_does_not_refire_while_staying_inside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_rule_evaluator_does_not_stamp_fired_when_device_discovery_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=300,
        device_state_getter=lambda: None,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    fire_state = fixture.evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at is None
    assert fire_state.last_error is not None
    assert "discovery still in progress" in fire_state.last_error.lower()


@pytest.mark.asyncio
async def test_rule_evaluator_records_error_when_notification_email_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    notify_rule = _arrive_home_rule(cooldown_s=300).model_copy(
        update={
            "device_actions": [],
            "notification_emails": ["ops@example.com"],
            "notify_on_fire": True,
        },
    )
    _write_bundle(bundle, notify_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0, reported_at=clock["now"] - 400.0,
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with (
        patch(
            "app.rule_evaluator.send_rule_notification_email",
            side_effect=RuleActionDispatchError("SMTP is not configured"),
        ),
        patch("app.rule_evaluator._LOGGER.warning") as warning_mock,
    ):
        await evaluator.on_location_update("henrique")

    fire_state = evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at is None
    assert fire_state.last_error == "SMTP is not configured"
    warning_mock.assert_called_once_with(
        "[rules] rule_id=%s edge matched but no side effect completed: %s",
        "arrive-home",
        "SMTP is not configured",
    )


@pytest.mark.asyncio
async def test_rule_evaluator_fires_on_geofence_enter_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=300,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]
    fire_state = fixture.evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at == fixture.clock["now"]


def test_user_triggered_geofence_edge_requires_enter_for_inside_rule() -> None:
    conditions = RuleConditionsOut(all=[_henrique_inside_house_condition()]).all
    assert _user_triggered_geofence_edge(
        conditions,
        "henrique",
        {"house": GeofenceTransition(entered=True)},
    )
    assert not _user_triggered_geofence_edge(
        conditions,
        "henrique",
        {"house": GeofenceTransition(left=True)},
    )


def _seed_henrique_inside_house(fixture: _ArriveHomeFixture) -> None:
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _move_henrique_outside_house(fixture: _ArriveHomeFixture) -> None:
    fixture.clock["now"] += 30.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )


@pytest.mark.asyncio
async def test_rule_evaluator_persists_fire_state_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=300,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")

    restarted = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: None,
        now_fn=lambda: fixture.clock["now"],
    )
    fire_state = restarted.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at == fixture.clock["now"]
    assert fire_state.last_error is None


@pytest.mark.asyncio
async def test_rule_evaluator_suppresses_geofence_reenter_within_dwell_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _seed_presence_db(
        fixture.db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
    )
    fixture.evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr([fixture.device]),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=fixture.db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: fixture.clock["now"],
    )

    _move_henrique_outside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")

    assert fixture.device.calls == []


@pytest.mark.asyncio
async def test_rule_evaluator_fires_geofence_reenter_after_dwell_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _seed_henrique_inside_house(fixture)
    fixture.evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr([fixture.device]),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=fixture.db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: fixture.clock["now"],
    )

    _move_henrique_outside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    fixture.clock["now"] += 310.0
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")

    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_rule_evaluator_seeds_geofence_inside_since_on_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot seeding sets ``inside_since`` when the latest location is inside."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    dwell_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="dwell-only",
        label="Dwell only",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    _write_bundle(bundle, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reported_at = 1_700_000_000.0
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=reported_at, reported_at=reported_at,
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: reported_at,
    )
    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): reported_at,
    }


@pytest.mark.asyncio
async def test_rule_evaluator_seeds_inside_since_from_history_streak_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boot seeding sets ``inside_since`` to the streak start, not the latest fix."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    dwell_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="dwell-only",
        label="Dwell only",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    _write_bundle(bundle, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    base = 1_700_000_000.0
    replace_users(
        db,
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
    for offset in (0, 300, 600, 900, 1200):
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id="henrique",
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=base + offset, reported_at=base + offset,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: base + 1200,
    )
    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): base,
    }


@pytest.mark.asyncio
async def test_rule_evaluator_seeds_outside_since_from_history_and_fires_enter_after_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Outside streak history survives restart so enter-edge rules fire without re-debouncing."""
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    outside_start = fixture.clock["now"] - 400.0
    for offset in (100, 200, 400):
        upsert_user_location(
            fixture.db,
            UserLocationRecord(
                user_id="henrique",
                lat=44.0,
                lon=-73.0,
                accuracy_m=20,
                fix_at=outside_start + offset, reported_at=outside_start + offset,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
    fixture.clock["now"] = outside_start + 400
    fixture.evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr([fixture.device]),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=fixture.db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: fixture.clock["now"],
    )
    assert fixture.evaluator.geofence_outside_since_snapshot() == {
        ("henrique", "house"): outside_start,
    }
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.evaluator.geofence_outside_since_snapshot() == {
        ("henrique", "house"): outside_start,
    }
    fixture.clock["now"] += 30.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_rule_evaluator_reconciles_outside_since_after_location_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """History reconcile restores a truncated away streak before enter-edge evaluation."""
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    outside_start = fixture.clock["now"] - 400.0
    for offset in (100, 200, 400):
        upsert_user_location(
            fixture.db,
            UserLocationRecord(
                user_id="henrique",
                lat=44.0,
                lon=-73.0,
                accuracy_m=20,
                fix_at=outside_start + offset, reported_at=outside_start + offset,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
    fixture.clock["now"] = outside_start + 400
    fixture.evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr([fixture.device]),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=fixture.db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: fixture.clock["now"],
    )
    key = ("henrique", "house")
    fixture.evaluator._geofence_outside_since[key] = fixture.clock["now"] - 61.0
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.evaluator.geofence_outside_since_snapshot() == {
        key: outside_start,
    }
    fixture.clock["now"] += 30.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_rule_evaluator_seeds_inside_since_when_dwell_accuracy_passes_edge_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    edge_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="edge-strict",
        label="Edge strict",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )
    dwell_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="dwell-loose",
        label="Dwell loose",
        min_location_accuracy_m=200,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    _write_bundle(bundle, edge_rule, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reported_at = 1_700_000_000.0
    replace_users(
        db,
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
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=120,
            fix_at=reported_at, reported_at=reported_at,
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: reported_at,
    )
    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): reported_at,
    }
    assert ("henrique", "house") not in evaluator._geofence_was_inside


@pytest.mark.asyncio
async def test_rule_evaluator_seeds_outside_since_when_dwell_accuracy_passes_edge_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    edge_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="edge-strict",
        label="Edge strict",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )
    dwell_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="dwell-loose",
        label="Dwell loose",
        min_location_accuracy_m=200,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    _write_bundle(bundle, edge_rule, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reported_at = 1_700_000_000.0
    replace_users(
        db,
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
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=120,
            fix_at=reported_at, reported_at=reported_at,
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: reported_at,
    )
    assert evaluator.geofence_outside_since_snapshot() == {
        ("henrique", "house"): reported_at,
    }


@pytest.mark.asyncio
async def test_rule_evaluator_tracks_inside_since_on_dwell_eligible_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_dwell_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _seed_henrique_inside_house(fixture)
    evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: None,
        now_fn=lambda: fixture.clock["now"],
    )
    enter_at = fixture.clock["now"]
    _move_henrique_outside_house(fixture)
    await evaluator.on_location_update("henrique")
    fixture.clock["now"] += 310.0
    _move_henrique_inside_house(fixture)
    await evaluator.on_location_update("henrique")

    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): fixture.clock["now"],
    }
    assert evaluator.geofence_inside_since_snapshot()[("henrique", "house")] > enter_at


@pytest.mark.asyncio
async def test_rule_evaluator_clears_inside_since_on_leave(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_dwell_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _seed_henrique_inside_house(fixture)
    evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: None,
        now_fn=lambda: fixture.clock["now"],
    )
    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): fixture.clock["now"],
    }

    _move_henrique_outside_house(fixture)
    await evaluator.on_location_update("henrique")

    assert evaluator.geofence_inside_since_snapshot() == {}


@pytest.mark.asyncio
async def test_rule_evaluator_sets_inside_since_on_debounced_reenter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_dwell_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _seed_henrique_inside_house(fixture)
    evaluator = RuleEvaluator(
        cache_path=fixture.db,
        device_state_getter=lambda: None,
        now_fn=lambda: fixture.clock["now"],
    )

    _move_henrique_outside_house(fixture)
    await evaluator.on_location_update("henrique")
    fixture.clock["now"] += 30.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    assert evaluator.geofence_inside_since_snapshot() == {
        ("henrique", "house"): fixture.clock["now"],
    }


@pytest.mark.asyncio
async def test_rule_evaluator_skips_inside_since_seed_when_no_dwell_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reported_at = 1_700_000_000.0
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=reported_at, reported_at=reported_at,
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: reported_at,
    )
    assert evaluator.geofence_inside_since_snapshot() == {}


@pytest.mark.asyncio
async def test_rule_evaluator_skips_inside_since_seed_when_dwell_rule_rejects_accuracy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    dwell_rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="dwell-only",
        label="Dwell only",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    _write_bundle(bundle, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    replace_users(
        db,
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
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=120,
            fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )
    assert evaluator.geofence_inside_since_snapshot() == {}


def _scheduled_inside_house_rule(
    *,
    cooldown_s: int,
    fire_once_per_local_day: bool = False,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[_henrique_inside_house_condition()]),
        cooldown_s=cooldown_s,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        fire_once_per_local_day=fire_once_per_local_day,
        id="scheduled-inside",
        label="Scheduled inside",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="* * * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )


def _setup_scheduled_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_s: int,
    fire_once_per_local_day: bool = False,
) -> _ArriveHomeFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(
        bundle,
        _scheduled_inside_house_rule(
            cooldown_s=cooldown_s,
            fire_once_per_local_day=fire_once_per_local_day,
        ),
    )
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"], reported_at=clock["now"],
    )
    device = _FakeKasa("192.168.1.10", "Garage")
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
    return _ArriveHomeFixture(
        clock=clock,
        db=db,
        device=device,
        evaluator=evaluator,
    )


@pytest.mark.asyncio
async def test_scheduled_rule_seeds_next_evaluate_at_on_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(tmp_path, monkeypatch, cooldown_s=0)
    next_at = fixture.evaluator.next_evaluate_at_for_rule("scheduled-inside")
    assert next_at is not None
    assert next_at >= fixture.clock["now"]


@pytest.mark.asyncio
async def test_scheduled_rule_fires_when_due_and_conditions_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(tmp_path, monkeypatch, cooldown_s=0)
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["on"]
    fire_state = fixture.evaluator.fire_state_for_rule("scheduled-inside")
    assert fire_state.last_fired_at is not None


@pytest.mark.asyncio
async def test_scheduled_rule_respects_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(tmp_path, monkeypatch, cooldown_s=600)
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    runtime.last_fired_at = fixture.clock["now"] - 30.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == []
    assert runtime.next_evaluate_at is not None
    assert runtime.next_evaluate_at > fixture.clock["now"]


@pytest.mark.asyncio
async def test_scheduled_rule_advances_next_evaluate_at_when_conditions_unmet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(tmp_path, monkeypatch, cooldown_s=0)
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=fixture.clock["now"], reported_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    previous_next = runtime.next_evaluate_at
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == []
    assert runtime.next_evaluate_at is not None
    assert runtime.next_evaluate_at > previous_next


@pytest.mark.asyncio
async def test_scheduled_rule_fire_once_per_local_day_failed_fire_does_not_consume_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
        fire_once_per_local_day=True,
    )
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    with patch(
        "app.rule_evaluator.dispatch_rule_device_actions",
        return_value=RuleDeviceDispatchResult(
            action_outcomes=(),
            errors=("Device not found: Garage",),
            probable_successes=(),
        ),
    ):
        await fixture.evaluator._evaluate_scheduled_rules()
    assert runtime.last_fired_at is None
    assert runtime.last_error == "Device not found: Garage"

    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["on"]
    assert runtime.last_fired_at == fixture.clock["now"]


@pytest.mark.asyncio
async def test_scheduled_rule_fire_once_per_local_day_fires_after_local_midnight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
        fire_once_per_local_day=True,
    )
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.last_fired_at = fixture.clock["now"] - 86_400.0
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_scheduled_rule_fire_once_per_local_day_fires_first_tick(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
        fire_once_per_local_day=True,
    )
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_scheduled_rule_fire_once_per_local_day_skips_second_tick_same_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_scheduled_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
        fire_once_per_local_day=True,
    )
    runtime = fixture.evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    runtime.last_fired_at = fixture.clock["now"] - 60.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == []


@pytest.mark.asyncio
async def test_rule_evaluator_records_fire_when_email_sent_despite_action_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    notify_rule = _arrive_home_rule(cooldown_s=300).model_copy(
        update={
            "device_actions": [
                RuleDeviceActionOut(
                    family_id=DeviceFamilyId.KASA,
                    device_id="Front door lights",
                    action=RuleDeviceActionType.TURN_ON,
                ),
            ],
            "notification_emails": ["ops@example.com"],
            "notify_on_fire": True,
        },
    )
    _write_bundle(bundle, notify_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    device = _FakeKasa("192.168.1.20", "Front door lights")
    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0, reported_at=clock["now"] - 400.0,
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr([device]),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with (
        patch(
            "app.rule_evaluator.dispatch_rule_device_actions",
            return_value=RuleDeviceDispatchResult(
                action_outcomes=(),
                errors=("Sonos zone 'Living Room' skipped: already paused",),
                probable_successes=(),
            ),
        ),
        patch(
            "app.rule_evaluator.send_rule_notification_email",
            return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
        ),
        patch("app.rule_evaluator._LOGGER.info") as info_mock,
        patch("app.rule_evaluator._LOGGER.warning") as warning_mock,
    ):
        await evaluator.on_location_update("henrique")

    fire_state = evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at == clock["now"]
    assert fire_state.last_error is not None
    assert "Living Room" in fire_state.last_error
    warning_mock.assert_called_once()
    assert "partial side-effect failures" in warning_mock.call_args.args[0]
    fired_logs = [
        call
        for call in info_mock.call_args_list
        if call.args
        and call.args[0]
        == (
            "[rules] fired rule_id=%s user_ids=%s source=%s transitions=%s "
            "conditions=%s actions=%d email=%s duration_ms=%.0f%s%s"
        )
    ]
    assert len(fired_logs) == 1


def _away_shutdown_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique"],
                ),
                DevicesAnyOnCondition(
                    type="devices_any_on",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Garage",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
        enabled=True,
        id="away-shutdown",
        label="Away shutdown",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="*/10 * * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )


def _setup_away_shutdown_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_s: int,
) -> _ArriveHomeFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_shutdown_rule(cooldown_s=cooldown_s))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"], reported_at=clock["now"],
    )
    device = _FakeKasa("192.168.1.10", "Garage", is_on=True)
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
    return _ArriveHomeFixture(
        clock=clock,
        db=db,
        device=device,
        evaluator=evaluator,
    )


@pytest.mark.asyncio
async def test_scheduled_outside_dwell_fires_once_per_away_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_away_shutdown_evaluator(tmp_path, monkeypatch, cooldown_s=0)
    runtime = fixture.evaluator._rule_state["away-shutdown"]
    fixture.clock["now"] += 1300.0
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0

    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["off"]

    fixture.clock["now"] += 600.0
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["off"]

    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    _move_henrique_outside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    fixture.device._on = True
    fixture.clock["now"] += 1300.0
    runtime.next_evaluate_at = fixture.clock["now"] - 1.0
    await fixture.evaluator._evaluate_scheduled_rules()
    assert fixture.device.calls == ["off", "off"]


@pytest.mark.asyncio
async def test_geofence_leave_resets_outside_since_on_redeparture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_dwell_home_evaluator(tmp_path, monkeypatch, cooldown_s=0)
    _seed_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    first_outside_at = fixture.clock["now"] + 30.0
    _move_henrique_outside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.evaluator.geofence_outside_since_snapshot()[
        ("henrique", "house")
    ] == pytest.approx(first_outside_at)

    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    assert ("henrique", "house") not in fixture.evaluator.geofence_outside_since_snapshot()

    second_outside_at = fixture.clock["now"] + 30.0
    _move_henrique_outside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    outside_since = fixture.evaluator.geofence_outside_since_snapshot()[
        ("henrique", "house")
    ]
    assert outside_since == pytest.approx(second_outside_at)
    assert outside_since > first_outside_at
