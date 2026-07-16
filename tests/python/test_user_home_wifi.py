"""Tests for per-user home WiFi roster fields."""

from __future__ import annotations

from pathlib import Path

from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, list_observed_wifi_networks_for_user, upsert_user_location
from app.rules_store import UserRecord, list_users, replace_users, set_user_home_wifi


def _henrique() -> UserRecord:
    return UserRecord(
        user_id="henrique",
        first_name="Henrique",
        last_name="",
        display_name="Henrique",
        tracking_device_label="Pixel",
        enabled=True,
        home_wifi_ssid=None,
        home_wifi_bssid=None,
    )


def test_replace_users_preserves_home_wifi(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_users(db, [_henrique()])
    set_user_home_wifi(
        db,
        "henrique",
        wifi_ssid="HomeNet",
        wifi_bssid="aa:bb:cc:dd:ee:ff",
    )
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
                home_wifi_ssid=None,
                home_wifi_bssid=None,
            ),
        ],
    )
    saved = list_users(db)[0]
    assert saved.home_wifi_ssid == "HomeNet"
    assert saved.home_wifi_bssid == "aa:bb:cc:dd:ee:ff"


def test_list_observed_wifi_networks_dedupes_by_bssid(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    retention = default_location_history_retention()
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=100.0,
            reported_at=100.0,
            source="my-tracks",
            wifi_ssid="OldLabel",
            wifi_bssid="aa:bb:cc:dd:ee:ff",
        ),
        retention=retention,
    )
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=200.0,
            reported_at=200.0,
            source="my-tracks",
            wifi_ssid="HomeNet",
            wifi_bssid="aa:bb:cc:dd:ee:ff",
        ),
        retention=retention,
    )
    networks = list_observed_wifi_networks_for_user(db, "henrique")
    assert len(networks) == 1
    assert networks[0].wifi_ssid == "HomeNet"
    assert networks[0].wifi_bssid == "aa:bb:cc:dd:ee:ff"
