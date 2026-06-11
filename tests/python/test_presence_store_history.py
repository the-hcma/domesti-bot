"""Tests for user location history retention."""

from __future__ import annotations

from pathlib import Path

from app.location_history_retention import default_location_history_retention
from app.presence_store import (
    UserLocationRecord,
    count_user_location_history,
    upsert_user_location,
)
from app.rules_store import UserRecord, replace_users


def _location_record(user_id: str, received_at: float) -> UserLocationRecord:
    return UserLocationRecord(
        user_id=user_id,
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=12,
        received_at=received_at,
        source="test",
    )


def test_upsert_appends_history_and_prunes_to_retention_policy(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Test",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
            ),
        ],
    )
    retention = default_location_history_retention()
    now = 1_700_000_000.0
    for index in reversed(range(25)):
        upsert_user_location(
            db,
            _location_record("henrique", now - (index * 10_000.0)),
            retention=retention,
        )
    assert count_user_location_history(db, "henrique") == 20
