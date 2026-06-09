"""Tests for persisted Automations participants and geofences."""

from __future__ import annotations

from pathlib import Path

from app.rules_store import (
    GeofenceRecord,
    ParticipantRecord,
    count_geofences,
    count_participants,
    delete_geofence,
    list_geofences,
    list_participants,
    replace_geofences,
    replace_participants,
    save_geofence,
)


def test_replace_and_list_participants(tmp_path: Path) -> None:
    db = tmp_path / "rules.sqlite"
    count = replace_participants(
        db,
        [
            ParticipantRecord(
                participant_id="henrique",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
            ),
        ],
    )
    assert count == 1
    assert count_participants(db) == 1
    rows = list_participants(db)
    assert rows[0].participant_id == "henrique"


def test_replace_and_list_geofences(tmp_path: Path) -> None:
    db = tmp_path / "rules.sqlite"
    count = replace_geofences(
        db,
        [
            GeofenceRecord(
                geofence_id="henrique-house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.888325,
                radius_m=250,
                enabled=True,
                owntracks_rid="rid-1",
            ),
        ],
    )
    assert count == 1
    assert count_geofences(db) == 1
    rows = list_geofences(db)
    assert rows[0].geofence_id == "henrique-house"


def test_save_and_delete_geofence(tmp_path: Path) -> None:
    db = tmp_path / "rules.sqlite"
    save_geofence(
        db,
        GeofenceRecord(
            geofence_id="office",
            label="Office",
            center_lat=40.0,
            center_lon=-74.0,
            radius_m=100,
            enabled=False,
            owntracks_rid=None,
        ),
    )
    assert count_geofences(db) == 1
    delete_geofence(db, "office")
    assert count_geofences(db) == 0
