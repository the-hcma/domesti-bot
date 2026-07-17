"""Hermetic tests for WiFi-at-home geofence presence reconciliation."""

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
    UsersInsideGeofenceForSCondition,
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


def _kasa_mgr(device: _FakeKasa) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = (device,)
    mgr.get_device_by_alias.return_value = None
    return cast(KasaDeviceManager, mgr)


def _seed_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    reported_at: float,
    accuracy_m: int | None,
    connection_type: str | None = None,
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
            connection_type=connection_type,
            fix_at=fix_epoch,
            reported_at=reported_at,
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
            "wifi_home_presence_enabled": True,
            "wifi_home_geofence_id": "house",
        },
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.asyncio
async def test_wifi_home_reconciles_after_mobile_leave_without_false_arrive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Jun-20-style replay: mobile away, WiFi resyncs home, later mobile GPS enter does not fire."""
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
            connection_type="m",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []

    clock["now"] += 19 * 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=300,
            connection_type="w",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    reconciled = [
        str(call.args[0] % call.args[1:])
        for call in info_mock.call_args_list
        if call.args and "wifi home presence reconciled" in str(call.args[0])
    ]
    assert reconciled
    assert device.calls == []

    clock["now"] += 3 * 3600.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19405,
            lon=-73.88827,
            accuracy_m=5,
            connection_type="m",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")
    assert device.calls == []


@pytest.mark.asyncio
async def test_wifi_far_from_home_does_not_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WiFi on a distant network must not sync home presence without home coordinates."""
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

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19167,
            lon=-73.88399,
            accuracy_m=4,
            connection_type="m",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.2000,
            lon=-73.9000,
            accuracy_m=300,
            connection_type="w",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    reconciled = [
        str(call.args[0] % call.args[1:])
        for call in info_mock.call_args_list
        if call.args and "wifi home presence reconciled" in str(call.args[0])
    ]
    assert reconciled == []
    assert device.calls == []


@pytest.mark.asyncio
async def test_wifi_home_seeds_dwell_inside_since_for_low_accuracy_wifi(
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
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="dwell-home",
        label="Dwell home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="* * * * *",
    )
    _write_bundle(bundle, _arrive_home_rule(), dwell_rule)
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
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.19167,
            lon=-73.88399,
            accuracy_m=4,
            connection_type="m",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    clock["now"] += 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=300,
            connection_type="w",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    inside_since = evaluator.geofence_inside_since_snapshot().get(("henrique", "house"))
    assert inside_since == clock["now"]
    override_logs = [
        call
        for call in info_mock.call_args_list
        if call.args and "wifi home presence overrode low-accuracy location" in str(call.args[0])
    ]
    assert override_logs


@pytest.mark.asyncio
async def test_wifi_home_reconcile_runs_for_dwell_only_bundle(
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
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="dwell-home",
        label="Dwell home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    _write_bundle(bundle, dwell_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_db(
        db,
        user_id="henrique",
        lat=41.19167,
        lon=-73.88399,
        fix_at=clock["now"] - 120.0,
        reported_at=clock["now"] - 120.0,
        accuracy_m=4,
        connection_type="m",
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")

    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=300,
            connection_type="w",
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    inside_since = evaluator.geofence_inside_since_snapshot().get(("henrique", "house"))
    assert inside_since == clock["now"]
