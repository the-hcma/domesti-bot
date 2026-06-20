"""Tests for OwnTracks presence connection type normalization."""

from __future__ import annotations

from app.presence_connection_type import (
    PresenceConnectionType,
    connection_type_is_wifi,
    connection_type_label_for_log,
    normalize_presence_connection_type,
)


def test_normalize_presence_connection_type_canonicalizes() -> None:
    assert normalize_presence_connection_type("W") == "w"
    assert normalize_presence_connection_type(" m ") == "m"
    assert normalize_presence_connection_type(None) is None
    assert normalize_presence_connection_type("") is None
    assert normalize_presence_connection_type("   ") is None
    assert normalize_presence_connection_type("x") is None


def test_connection_type_is_wifi() -> None:
    assert connection_type_is_wifi(PresenceConnectionType.WIFI) is True
    assert connection_type_is_wifi("m") is False
    assert connection_type_is_wifi(None) is False


def test_connection_type_label_for_log() -> None:
    assert connection_type_label_for_log("w") == "wifi"
    assert connection_type_label_for_log(None) == "unknown"
