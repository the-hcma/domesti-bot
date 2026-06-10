"""Tests for persisted participant location fixes."""

from __future__ import annotations

from pathlib import Path

from app.presence_store import (
    ParticipantFixRecord,
    geofence_ids_containing_fix,
    list_participant_fixes,
    replace_participant_fixes,
)
from app.rules_store import GeofenceRecord, list_geofences, replace_geofences


def test_replace_and_list_participant_fixes(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_participant_fixes(
        db,
        [
            ParticipantFixRecord(
                participant_id="henrique",
                lat=41.194072,
                lon=-73.888325,
                accuracy_m=12,
                received_at=1_700_000_000.0,
                source="my-tracks",
            ),
        ],
    )
    fixes = list_participant_fixes(db)
    assert fixes["henrique"].lat == 41.194072
    assert fixes["henrique"].source == "my-tracks"


def test_geofence_ids_containing_fix(tmp_path: Path) -> None:
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
    fix = ParticipantFixRecord(
        participant_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=12,
        received_at=1_700_000_000.0,
        source="my-tracks",
    )
    inside = geofence_ids_containing_fix(fix, list_geofences(db))
    assert inside == ["house"]
