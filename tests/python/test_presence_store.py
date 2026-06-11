"""Tests for persisted user locations."""

from __future__ import annotations

from pathlib import Path

from app.location_history_retention import default_location_history_retention
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_locations,
    replace_user_locations,
)
from app.rules_store import GeofenceRecord, list_geofences, replace_geofences


def test_replace_and_list_user_locations(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_user_locations(
        db,
        [
            UserLocationRecord(
                user_id="henrique",
                lat=41.194072,
                lon=-73.888325,
                accuracy_m=12,
                received_at=1_700_000_000.0,
                source="my-tracks",
            ),
        ],
        retention=default_location_history_retention(),
    )
    locations = list_user_locations(db)
    assert locations["henrique"].lat == 41.194072
    assert locations["henrique"].source == "my-tracks"


def test_geofence_ids_containing_location(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
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
    location = UserLocationRecord(
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=12,
        received_at=1_700_000_000.0,
        source="my-tracks",
    )
    inside = geofence_ids_containing_location(location, list_geofences(db))
    assert inside == ["house"]
