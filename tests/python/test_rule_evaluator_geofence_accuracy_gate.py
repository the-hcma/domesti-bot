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
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
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
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")


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
        trigger="edge_true",
    )


def _seed_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    received_at: float,
    accuracy_m: int | None,
) -> None:
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
            received_at=received_at,
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
        received_at=clock["now"],
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
            received_at=clock["now"],
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
            received_at=clock["now"],
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
        received_at=clock["now"] - 400.0,
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
            received_at=clock["now"],
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
            received_at=clock["now"],
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
        received_at=clock["now"] - 400.0,
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
            received_at=clock["now"],
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
        received_at=clock["now"] - 400.0,
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
            received_at=clock["now"],
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
            received_at=clock["now"],
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
