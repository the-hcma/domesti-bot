"""Tests for household membership on the automation roster."""

from __future__ import annotations

from pathlib import Path

from app.rules_store import (
    UserRecord,
    list_users,
    replace_users,
    set_user_home_wifi,
    set_user_household,
)


def _henrique(*, is_household: bool = False) -> UserRecord:
    return UserRecord(
        user_id="henrique",
        first_name="Henrique",
        last_name="",
        display_name="Henrique",
        tracking_device_label="Pixel",
        enabled=True,
        home_wifi_ssid=None,
        home_wifi_bssid=None,
        is_household=is_household,
    )


def test_replace_users_preserves_household_flag(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_users(db, [_henrique()])
    set_user_household(db, "henrique", is_household=True)
    replace_users(db, [_henrique(is_household=False)])
    saved = list_users(db)[0]
    assert saved.is_household is True


def test_set_user_household_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_users(db, [_henrique()])
    marked = set_user_household(db, "henrique", is_household=True)
    assert marked.is_household is True
    cleared = set_user_household(db, "henrique", is_household=False)
    assert cleared.is_household is False


def test_replace_users_preserves_home_wifi_and_household(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_users(db, [_henrique()])
    set_user_household(db, "henrique", is_household=True)
    set_user_home_wifi(
        db,
        "henrique",
        wifi_ssid="HomeNet",
        wifi_bssid="aa:bb:cc:dd:ee:ff",
    )
    replace_users(db, [_henrique()])
    saved = list_users(db)[0]
    assert saved.is_household is True
    assert saved.home_wifi_ssid == "HomeNet"
    assert saved.home_wifi_bssid == "aa:bb:cc:dd:ee:ff"
