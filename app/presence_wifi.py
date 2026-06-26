"""WiFi network helpers for presence locations."""

from __future__ import annotations


def normalize_wifi_bssid(value: str | None) -> str | None:
    """Return a canonical lowercase BSSID, or ``None`` when absent."""
    if value is None:
        return None
    trimmed = value.strip().lower()
    if trimmed == "":
        return None
    return trimmed


def wifi_bssids_match(
    observed_bssid: str | None,
    home_bssid: str | None,
) -> bool:
    """Return whether both BSSIDs are present and equal after normalization."""
    normalized_observed = normalize_wifi_bssid(observed_bssid)
    normalized_home = normalize_wifi_bssid(home_bssid)
    if normalized_observed is None or normalized_home is None:
        return False
    return normalized_observed == normalized_home
