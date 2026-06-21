"""OwnTracks ``conn`` codes for user presence locations."""

from __future__ import annotations

from enum import StrEnum


class PresenceConnectionType(StrEnum):
    """Canonical OwnTracks connection codes stored in SQLite."""

    WIFI = "w"
    MOBILE = "m"
    OFFLINE = "o"


_CONNECTION_TYPE_LABELS: dict[str, str] = {
    PresenceConnectionType.WIFI: "wifi",
    PresenceConnectionType.MOBILE: "mobile",
    PresenceConnectionType.OFFLINE: "offline",
}


def connection_type_is_wifi(connection_type: str | None) -> bool:
    """Return whether ``connection_type`` is stored WiFi (``w``)."""
    return connection_type == PresenceConnectionType.WIFI


def connection_type_label_for_log(connection_type: str | None) -> str:
    """Map a stored OwnTracks ``conn`` code to a presence log label."""
    if connection_type is None:
        return "unknown"
    return _CONNECTION_TYPE_LABELS.get(connection_type, connection_type)


def normalize_presence_connection_type(connection_type: str | None) -> str | None:
    """Return a canonical lowercase OwnTracks ``conn`` code, or ``None`` when absent/invalid."""
    if connection_type is None:
        return None
    code = connection_type.strip().lower()
    if code == "":
        return None
    try:
        return PresenceConnectionType(code).value
    except ValueError:
        return None
