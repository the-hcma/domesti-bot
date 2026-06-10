"""Tests for participant location history retention."""

from __future__ import annotations

from pathlib import Path

from app.location_history_retention import default_location_history_retention
from app.presence_store import (
    ParticipantFixRecord,
    count_participant_location_history,
    upsert_participant_fix,
)
from app.rules_store import ParticipantRecord, replace_participants


def _fix(participant_id: str, received_at: float) -> ParticipantFixRecord:
    return ParticipantFixRecord(
        participant_id=participant_id,
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=12,
        received_at=received_at,
        source="test",
    )


def test_upsert_appends_history_and_prunes_to_retention_policy(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_participants(
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
    retention = default_location_history_retention()
    now = 1_700_000_000.0
    for index in reversed(range(25)):
        upsert_participant_fix(
            db,
            _fix("henrique", now - (index * 10_000.0)),
            retention=retention,
        )
    assert count_participant_location_history(db, "henrique") == 20
