"""Rules status user rows should match WiFi-aware effective geofence presence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rules_status import build_rules_status
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


def _write_bundle(path: Path) -> None:
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
        "rules": [
            {
                "id": "evening-lights-off-both-home",
                "label": "Lights off",
                "enabled": True,
                "triggers": ["scheduled"],
                "schedule_cron": "*/10 * * * *",
                "cooldown_s": 300,
                "min_location_accuracy_m": 50,
                "notify_on_fire": False,
                "conditions": {
                    "all": [
                        {
                            "type": "users_inside_geofence_for_s",
                            "geofence_id": "house",
                            "user_ids": ["kristen"],
                            "min_inside_s": 600,
                        },
                    ],
                },
                "device_actions": [],
            },
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_build_rules_status_user_inside_geofence_ids_include_wifi_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    replace_users(
        db,
        [
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
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="kristen",
            lat=41.1941344,
            lon=-73.8882358,
            accuracy_m=97,
            connection_type="w",
            fix_at=1_700_000_000.0,
            reported_at=1_700_000_000.0,
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    status = build_rules_status(cache_path=db)
    kristen = next(user for user in status.users if user.user_id == "kristen")
    assert kristen.inside_geofence_ids == ["house"]
