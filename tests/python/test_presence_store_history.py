"""Tests for user location history retention."""

from __future__ import annotations

from pathlib import Path

from app.location_history_retention import default_location_history_retention
from app.presence_store import (
    UserLocationRecord,
    count_user_location_history,
    list_user_location_history_for_user,
    list_user_location_history_for_walkback,
    list_user_location_history_for_walkback_by_user,
    upsert_user_location,
)
from app.rules_store import UserRecord, replace_users


def _location_record(user_id: str, reported_at: float) -> UserLocationRecord:
    """Build a test fixture location near the house geofence."""
    return UserLocationRecord(
        user_id=user_id,
        lat=41.194085,
        lon=-73.888365,
        accuracy_m=12,
        fix_at=reported_at, reported_at=reported_at,
        source="test",
    )


def test_upsert_appends_history_and_prunes_to_retention_policy(tmp_path: Path) -> None:
    """Each upsert appends history and pruning keeps the retention union policy."""
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


def test_list_user_location_history_for_user_filters_since(tmp_path: Path) -> None:
    """``since`` excludes history rows older than the cutoff epoch."""
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
    base = 1_700_000_000.0
    upsert_user_location(
        db,
        _location_record("henrique", base - 5_000.0),
        retention=retention,
    )
    upsert_user_location(
        db,
        _location_record("henrique", base - 1_000.0),
        retention=retention,
    )
    upsert_user_location(
        db,
        _location_record("henrique", base),
        retention=retention,
    )
    rows = list_user_location_history_for_user(
        db,
        "henrique",
        since=base - 2_000.0,
    )
    assert [row.reported_at for row in rows] == [base - 1_000.0, base]


def test_list_user_location_history_for_walkback_newest_first_within_window(
    tmp_path: Path,
) -> None:
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
    walkback_max_s = 600.0
    for offset_s in (900.0, 300.0, 30.0):
        upsert_user_location(
            db,
            _location_record("henrique", now - offset_s),
            retention=retention,
        )
    rows = list_user_location_history_for_walkback(
        db,
        "henrique",
        now_epoch=now,
        walkback_max_s=walkback_max_s,
        limit=10,
    )
    assert [row.reported_at for row in rows] == [now - 30.0, now - 300.0]


def test_list_user_location_history_for_walkback_returns_all_rows_in_window_by_default(
    tmp_path: Path,
) -> None:
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
    walkback_max_s = 600.0
    for offset_s in (590.0, 300.0, 30.0):
        upsert_user_location(
            db,
            _location_record("henrique", now - offset_s),
            retention=retention,
        )
    rows = list_user_location_history_for_walkback(
        db,
        "henrique",
        now_epoch=now,
        walkback_max_s=walkback_max_s,
    )
    assert [row.reported_at for row in rows] == [
        now - 30.0,
        now - 300.0,
        now - 590.0,
    ]


def test_list_user_location_history_for_walkback_by_user_loads_all_users_once(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
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
            ),
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
                tracking_device_label="iPhone",
                enabled=True,
            ),
        ],
    )
    retention = default_location_history_retention()
    now = 1_700_000_000.0
    walkback_max_s = 600.0
    upsert_user_location(
        db,
        _location_record("henrique", now - 30.0),
        retention=retention,
    )
    upsert_user_location(
        db,
        _location_record("kristen", now - 45.0),
        retention=retention,
    )
    rows_by_user = list_user_location_history_for_walkback_by_user(
        db,
        {"henrique", "kristen"},
        now_epoch=now,
        walkback_max_s=walkback_max_s,
    )
    assert [row.reported_at for row in rows_by_user["henrique"]] == [now - 30.0]
    assert [row.reported_at for row in rows_by_user["kristen"]] == [now - 45.0]
