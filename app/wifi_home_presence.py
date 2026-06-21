"""WiFi-at-home presence assumptions for geofence evaluation."""

from __future__ import annotations

from collections.abc import Sequence

from app.api.schemas import GeofenceOut, SettingsLocationOut
from app.presence_connection_type import connection_type_is_wifi
from app.presence_store import UserLocationRecord, _haversine_m
from app.rules_store import GeofenceRecord

GeofencePresenceTarget = GeofenceRecord | GeofenceOut

WIFI_HOME_GEOFENCE_RADIUS_SCALE = 1.2


def _geofence_ids_containing_point(
    lat: float,
    lon: float,
    geofences: Sequence[GeofencePresenceTarget],
) -> frozenset[str]:
    probe = UserLocationRecord(
        user_id="",
        lat=lat,
        lon=lon,
        accuracy_m=None,
        received_at=0.0,
        source=None,
    )
    inside: list[str] = []
    for geofence in geofences:
        if not geofence.enabled:
            continue
        distance_m = _haversine_m(
            probe.lat,
            probe.lon,
            geofence.center_lat,
            geofence.center_lon,
        )
        if distance_m <= float(geofence.radius_m):
            inside.append(geofence.geofence_id)
    return frozenset(inside)


def location_accuracy_is_low(
    accuracy_m: int | None,
    min_accuracy_m: int,
) -> bool:
    """Return whether ``accuracy_m`` exceeds the rule accuracy ceiling."""
    return accuracy_m is not None and accuracy_m > min_accuracy_m


def location_within_wifi_home_proximity(
    lat: float,
    lon: float,
    geofence: GeofencePresenceTarget,
) -> bool:
    """Return whether ``(lat, lon)`` is within geofence radius + 20% from center."""
    if not geofence.enabled:
        return False
    distance_m = _haversine_m(
        lat,
        lon,
        geofence.center_lat,
        geofence.center_lon,
    )
    return distance_m <= float(geofence.radius_m) * WIFI_HOME_GEOFENCE_RADIUS_SCALE


def wifi_home_geofence_ids(
    settings: SettingsLocationOut,
    geofences: Sequence[GeofencePresenceTarget],
) -> frozenset[str]:
    """Return geofence ids eligible for WiFi-at-home presence reconciliation."""
    if not settings.wifi_home_presence_enabled:
        return frozenset()
    enabled = [row for row in geofences if row.enabled]
    if not enabled:
        return frozenset()
    explicit = (settings.wifi_home_geofence_id or "").strip()
    if explicit:
        if any(row.geofence_id == explicit for row in enabled):
            return frozenset({explicit})
        return frozenset()
    return _geofence_ids_containing_point(settings.lat, settings.lon, enabled)


def wifi_home_presence_applies(
    settings: SettingsLocationOut,
    geofence_id: str,
    connection_type: str | None,
    *,
    accuracy_m: int | None,
    geofences: Sequence[GeofencePresenceTarget],
    lat: float,
    lon: float,
    min_accuracy_m: int,
) -> bool:
    """Return whether a low-accuracy WiFi fix should be treated as inside ``geofence_id``.

    Requires ``conn=w``, accuracy worse than ``min_accuracy_m``, and coordinates
    within the home geofence radius plus 20% slack from center.
    """
    if not connection_type_is_wifi(connection_type):
        return False
    if not location_accuracy_is_low(accuracy_m, min_accuracy_m):
        return False
    if geofence_id not in wifi_home_geofence_ids(settings, geofences):
        return False
    for geofence in geofences:
        if geofence.geofence_id != geofence_id or not geofence.enabled:
            continue
        return location_within_wifi_home_proximity(lat, lon, geofence)
    return False
