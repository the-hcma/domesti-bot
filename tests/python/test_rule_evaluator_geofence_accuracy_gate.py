"""Hermetic tests for geofence edge state accuracy gating."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
    UsersOutsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


class _FakeKasa:
    def __init__(self, host: str, label: str) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.host = host
        self.mac_address = None
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")

    async def turn_off(self) -> None:
        self.calls.append("off")


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


def _kasa_mgr(device: _FakeKasa) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = (device,)
    mgr.get_device_by_alias.return_value = None
    return cast(KasaDeviceManager, mgr)


def _arrive_home_rule() -> RuleOut:
    return RuleOut(
        accuracy_edge_grace_s=120,
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


def _leave_home_rule() -> RuleOut:
    return RuleOut(
        accuracy_edge_grace_s=120,
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceCondition(
                    type="users_outside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
        enabled=True,
        id="leave-home",
        label="Leave home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def _seed_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    reported_at: float,
    accuracy_m: int | None,
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
            accuracy_m=accuracy_m,
            fix_at=fix_epoch,
            reported_at=reported_at,
            source="test",
        ),
        retention=default_location_history_retention(),
    )


@pytest.mark.asyncio
async def test_low_accuracy_outside_fix_does_not_flip_inside_state_or_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"],
        reported_at=clock["now"],
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19167,
            lon=-73.88399,
            accuracy_m=300,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19423,
            lon=-73.88822,
            accuracy_m=12,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []


@pytest.mark.asyncio
async def test_low_accuracy_enter_waits_for_good_accuracy_fix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Low-accuracy fixes do not emit geofence edges or deferred grace intents."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0,
        reported_at=clock["now"] - 400.0,
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
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
            lat=41.19423,
            lon=-73.88822,
            accuracy_m=120,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 30.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19423,
            lon=-73.88822,
            accuracy_m=20,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_low_accuracy_inside_does_not_register_deferred_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0,
        reported_at=clock["now"] - 400.0,
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
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
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=100,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    deferred = [
        str(call.args[0] % call.args[1:])
        for call in info_mock.call_args_list
        if call.args and "deferred edge registered" in str(call.args[0])
    ]
    assert deferred == []
    assert device.calls == []


@pytest.mark.asyncio
async def test_good_leave_then_bad_inside_does_not_defer_arrive_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: bad-accuracy inside after a good leave must not register deferred edges."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"] - 400.0,
        reported_at=clock["now"] - 400.0,
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19167,
            lon=-73.88399,
            accuracy_m=4,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 3 * 3600.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=100,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    deferred = [
        str(call.args[0] % call.args[1:])
        for call in info_mock.call_args_list
        if call.args and "deferred edge registered" in str(call.args[0])
    ]
    assert deferred == []
    assert device.calls == []


@pytest.mark.asyncio
async def test_prolonged_geo_inside_streak_fires_arrive_after_accurate_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sustained GPS-inside with poor accuracy reconciles an enter edge; a later accurate location fires."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"],
        reported_at=clock["now"],
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.20693,
            lon=-73.89602,
            accuracy_m=4,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=100,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 601.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=100,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=5,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_prolonged_geo_outside_streak_fires_leave_after_accurate_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sustained GPS-outside with poor accuracy reconciles a leave edge; a later accurate location fires."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _leave_home_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"],
        reported_at=clock["now"],
        accuracy_m=20,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.20693,
            lon=-73.89602,
            accuracy_m=300,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 601.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.20693,
            lon=-73.89602,
            accuracy_m=300,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.20693,
            lon=-73.89602,
            accuracy_m=12,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == ["off"]
