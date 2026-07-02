"""Hermetic tests for proactive location monitoring."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.location_monitoring_policy import (
    LocationMonitoringPolicy,
    _effective_approach_request_interval_s,
    _is_in_approach_corridor,
)
from app.location_request_coordinator import LocationRequestCoordinator
from app.location_request_rate_limits import LocationRequestRateLimits
from app.location_history_retention import default_location_history_retention
from app.mytracks_store import (
    MyTracksConfigSave,
    save_mytracks_config,
    set_location_request_rate_limits,
)
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


def _write_edge_rule(path: Path) -> None:
    rule = RuleOut(
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


def _write_scheduled_rule_for_user(path: Path, user_id: str) -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=[user_id],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="morning-lights",
        label="Morning lights",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="0 8 * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )
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


def _house_geofence() -> GeofenceRecord:
    return GeofenceRecord(
        geofence_id="house",
        label="House",
        center_lat=41.194072,
        center_lon=-73.888325,
        radius_m=250,
        enabled=True,
        owntracks_rid=None,
    )


def _location_outside_in_corridor() -> UserLocationRecord:
    # ~300 m north of center => ~50 m outside a 250 m radius geofence.
    return UserLocationRecord(
        user_id="henrique",
        lat=41.196769,
        lon=-73.888325,
        accuracy_m=80,
        fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
        source="test",
    )


def test_is_in_approach_corridor_when_outside_but_within_buffer() -> None:
    geofence = _house_geofence()
    location = _location_outside_in_corridor()
    assert _is_in_approach_corridor(
        location,
        geofence,
        approach_distance_m=500,
    )


def test_is_not_in_approach_corridor_when_inside_geofence() -> None:
    geofence = _house_geofence()
    location = UserLocationRecord(
        user_id="henrique",
        lat=41.194072,
        lon=-73.888325,
        accuracy_m=12,
        fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
        source="test",
    )
    assert not _is_in_approach_corridor(
        location,
        geofence,
        approach_distance_m=500,
    )


def test_effective_approach_interval_uses_mytracks_reason_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "discovery.sqlite"
    save_mytracks_config(
        db,
        MyTracksConfigSave(domain="https://tracks.example.com", username="admin"),
    )
    set_location_request_rate_limits(
        db,
        limits=LocationRequestRateLimits(
            device_cooldown_seconds=60,
            user_cooldown_seconds=30,
            user_cooldown_seconds_by_reason={"approach_monitoring": 45},
        ),
    )
    monkeypatch.setattr(
        "app.location_monitoring_policy._APPROACH_REQUEST_INTERVAL_S",
        5.0,
    )
    assert _effective_approach_request_interval_s(db) == 45.0


def test_on_location_updated_enters_approach_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
                home_wifi_bssid=None,
            ),
        ],
    )
    replace_geofences(db, [_house_geofence()])
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    coordinator = LocationRequestCoordinator(cache_path=db)
    policy = LocationMonitoringPolicy(
        cache_path=db,
        coordinator=coordinator,
        deferred_edges_for_user=lambda _user_id: (),
        now_fn=lambda: 1_700_000_100.0,
    )
    location = _location_outside_in_corridor()
    upsert_user_location(
        db,
        location,
        retention=default_location_history_retention(),
    )

    policy.on_location_updated("henrique", location=location, now=1_700_000_100.0)
    assert ("henrique", "house") in policy._approach_by_key


def test_seed_approach_from_stored_locations_on_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_edge_rule(bundle)
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
                home_wifi_bssid=None,
            ),
        ],
    )
    replace_geofences(db, [_house_geofence()])
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: 1_700_000_100.0)
    policy = LocationMonitoringPolicy(
        cache_path=db,
        coordinator=coordinator,
        deferred_edges_for_user=lambda _user_id: (),
        now_fn=lambda: 1_700_000_100.0,
    )
    upsert_user_location(
        db,
        _location_outside_in_corridor(),
        retention=default_location_history_retention(),
    )

    policy._seed_approach_from_stored_locations()

    assert ("henrique", "house") in policy._approach_by_key


@pytest.mark.asyncio
async def test_stale_watchdog_schedules_request_without_edge_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_scheduled_rule_for_user(bundle, "henrique")
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
                home_wifi_bssid=None,
            ),
        ],
    )
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr("app.location_monitoring_policy._STALE_INTERVAL_S", 60.0)

    coordinator = LocationRequestCoordinator(cache_path=db, now_fn=lambda: 1_700_000_200.0)
    schedule_mock = MagicMock()
    coordinator.schedule_request_with_reason = schedule_mock  # type: ignore[method-assign]

    policy = LocationMonitoringPolicy(
        cache_path=db,
        coordinator=coordinator,
        deferred_edges_for_user=lambda _user_id: (),
        now_fn=lambda: 1_700_000_200.0,
    )
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=12,
            fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    await policy._run_stale_watchdog_tick()

    schedule_mock.assert_called_once()
    assert schedule_mock.call_args.kwargs["reason"] == "stale_watchdog"
    assert schedule_mock.call_args.kwargs["require_edge_rules"] is False
