"""Tests for WiFi BSSID normalization."""

from __future__ import annotations

from app.presence_wifi import (
    normalize_wifi_bssid,
    normalize_wifi_ssid,
    wifi_bssids_match,
    wifi_ssids_match,
)


def test_normalize_wifi_bssid_lowercases_and_trims() -> None:
    assert normalize_wifi_bssid(" AA:BB:CC:DD:EE:FF ") == "aa:bb:cc:dd:ee:ff"


def test_normalize_wifi_bssid_returns_none_for_blank() -> None:
    assert normalize_wifi_bssid("") is None
    assert normalize_wifi_bssid("   ") is None
    assert normalize_wifi_bssid(None) is None


def test_normalize_wifi_ssid_trims() -> None:
    assert normalize_wifi_ssid(" familia ") == "familia"
    assert normalize_wifi_ssid("") is None
    assert normalize_wifi_ssid(None) is None


def test_wifi_bssids_match_requires_both_values() -> None:
    assert wifi_bssids_match("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF") is True
    assert wifi_bssids_match(None, "aa:bb:cc:dd:ee:ff") is False
    assert wifi_bssids_match("aa:bb:cc:dd:ee:ff", None) is False
    assert wifi_bssids_match("aa:bb:cc:dd:ee:00", "aa:bb:cc:dd:ee:ff") is False


def test_wifi_ssids_match_requires_both_values() -> None:
    assert wifi_ssids_match("familia", "familia") is True
    assert wifi_ssids_match(" familia ", "familia") is True
    assert wifi_ssids_match(None, "familia") is False
    assert wifi_ssids_match("familia", None) is False
    assert wifi_ssids_match("familia", "guest") is False
