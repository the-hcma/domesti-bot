"""Tests for persisted user locations."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.location_history_retention import default_location_history_retention
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_locations,
    replace_user_locations,
    upsert_user_location,
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
                fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
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
        fix_at=1_700_000_000.0, reported_at=1_700_000_000.0,
        source="my-tracks",
    )
    inside = geofence_ids_containing_location(location, list_geofences(db))
    assert inside == ["house"]


def test_upsert_user_location_log_includes_accuracy_m(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.20665,
        lon=-73.89559,
        accuracy_m=35,
        fix_at=1_718_377_050.0, reported_at=1_718_377_050.0,
        source="my-tracks",
    )
    with patch("app.presence_store._LOCATION_LOGGER.info") as info_mock:
        upsert_user_location(
            db,
            location,
            retention=default_location_history_retention(),
        )

    info_mock.assert_called_once()
    message = info_mock.call_args[0][0] % info_mock.call_args[0][1:]
    assert "accuracy_m=35" in message
    assert "connection_type=unknown" in message
    assert "hcma" in message


def test_upsert_user_location_log_includes_connection_type(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.19405,
        lon=-73.88826,
        accuracy_m=11,
        fix_at=1_718_377_050.0, reported_at=1_718_377_050.0,
        source="my-tracks",
        connection_type="w",
    )
    with patch("app.presence_store._LOCATION_LOGGER.info") as info_mock:
        upsert_user_location(
            db,
            location,
            retention=default_location_history_retention(),
        )

    message = info_mock.call_args[0][0] % info_mock.call_args[0][1:]
    assert "connection_type=wifi" in message
    assert "accuracy_m=11" in message


def test_upsert_user_location_log_shows_unknown_when_accuracy_missing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.20665,
        lon=-73.89559,
        accuracy_m=None,
        fix_at=1_718_377_050.0, reported_at=1_718_377_050.0,
        source="my-tracks",
    )
    with patch("app.presence_store._LOCATION_LOGGER.info") as info_mock:
        upsert_user_location(
            db,
            location,
            retention=default_location_history_retention(),
        )

    message = info_mock.call_args[0][0] % info_mock.call_args[0][1:]
    assert "connection_type=unknown" in message


def test_upsert_user_location_log_includes_report_and_fix_times(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.19405,
        lon=-73.88826,
        accuracy_m=11,
        fix_at=1_718_993_280.0,
        reported_at=1_719_000_000.0,
        source="my-tracks",
        trigger="p",
    )
    with patch("app.presence_store._LOCATION_LOGGER.info") as info_mock:
        upsert_user_location(
            db,
            location,
            retention=default_location_history_retention(),
        )

    message = info_mock.call_args[0][0] % info_mock.call_args[0][1:]
    assert "report_at=" in message
    assert "fix_at=" in message
    assert "fix_was=" in message


def test_upsert_user_location_rejects_stale_report(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    newer = UserLocationRecord(
        user_id="hcma",
        lat=41.1,
        lon=-73.8,
        accuracy_m=12,
        fix_at=1_719_000_000.0,
        reported_at=1_719_000_000.0,
        source="my-tracks",
    )
    older = UserLocationRecord(
        user_id="hcma",
        lat=41.9,
        lon=-73.9,
        accuracy_m=12,
        fix_at=1_719_010_000.0,
        reported_at=1_718_000_000.0,
        source="my-tracks",
    )
    upsert_user_location(db, newer, retention=default_location_history_retention())
    stored = upsert_user_location(db, older, retention=default_location_history_retention())
    assert stored is False
    assert list_user_locations(db)["hcma"].lat == 41.1


def test_upsert_user_location_persists_wifi_metadata(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.19405,
        lon=-73.88826,
        accuracy_m=11,
        fix_at=1_718_377_050.0, reported_at=1_718_377_050.0,
        source="my-tracks",
        connection_type="w",
        fix_source="w",
        trigger="p",
        wifi_bssid="AA:BB:CC:DD:EE:FF",
        wifi_ssid="HomeNet",
        battery_level=82,
    )
    upsert_user_location(
        db,
        location,
        retention=default_location_history_retention(),
    )
    stored = list_user_locations(db)["hcma"]
    assert stored.wifi_ssid == "HomeNet"
    assert stored.wifi_bssid == "aa:bb:cc:dd:ee:ff"
    assert stored.fix_source == "w"
    assert stored.trigger == "p"
    assert stored.battery_level == 82


def test_upsert_user_location_log_includes_wifi_metadata(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    location = UserLocationRecord(
        user_id="hcma",
        lat=41.19405,
        lon=-73.88826,
        accuracy_m=11,
        fix_at=1_718_377_050.0, reported_at=1_718_377_050.0,
        source="my-tracks",
        connection_type="w",
        fix_source="w",
        trigger="p",
        wifi_bssid="aa:bb:cc:dd:ee:ff",
        wifi_ssid="HomeNet",
    )
    with patch("app.presence_store._LOCATION_LOGGER.info") as info_mock:
        upsert_user_location(
            db,
            location,
            retention=default_location_history_retention(),
        )

    message = info_mock.call_args[0][0] % info_mock.call_args[0][1:]
    assert "wifi_ssid='HomeNet'" in message
    assert "wifi_bssid=aa:bb:cc:dd:ee:ff" in message
    assert "fix_source='w'" in message
    assert "trigger='p'" in message
