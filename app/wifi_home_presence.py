"""WiFi-at-home presence assumptions for geofence evaluation."""

from __future__ import annotations

from collections.abc import Sequence

from app.api.schemas import GeofenceOut, RuleOut, SettingsLocationOut
from app.presence_connection_type import connection_type_is_wifi
from app.presence_store import UserLocationRecord, _haversine_m, geofence_ids_containing_location
from app.presence_wifi import normalize_wifi_bssid, wifi_bssids_match
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
        fix_at=0.0,
        reported_at=0.0,
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


def effective_geofence_ids_containing_location(
    location: UserLocationRecord,
    geofences: Sequence[GeofenceRecord],
    *,
    settings: SettingsLocationOut,
    min_accuracy_m: int | None,
    home_wifi_bssid: str | None = None,
) -> list[str]:
    """Return geofence ids that contain ``location``, including WiFi home presence."""
    if min_accuracy_m is None:
        return geofence_ids_containing_location(location, list(geofences))
    inside: list[str] = []
    geofence_list = list(geofences)
    for geofence in geofence_list:
        if not geofence.enabled:
            continue
        geofence_id = geofence.geofence_id
        if wifi_home_presence_applies(
            settings,
            geofence_id,
            location.connection_type,
            accuracy_m=location.accuracy_m,
            geofences=geofence_list,
            lat=location.lat,
            lon=location.lon,
            min_accuracy_m=min_accuracy_m,
            home_wifi_bssid=home_wifi_bssid,
            observed_wifi_bssid=location.wifi_bssid,
        ):
            inside.append(geofence_id)
            continue
        if location.accuracy_m is not None and location.accuracy_m > min_accuracy_m:
            continue
        if geofence_id in geofence_ids_containing_location(location, [geofence]):
            inside.append(geofence_id)
    return inside


def geofence_presence_accuracy_limit_m(rules: Sequence[RuleOut]) -> int | None:
    """Return the strictest accuracy limit among enabled automation rules."""
    limits = [rule.min_location_accuracy_m for rule in rules if rule.enabled]
    if not limits:
        return None
    return min(limits)


def history_row_geofence_inside(
    row: UserLocationRecord,
    geofence: GeofenceRecord,
    geofences: Sequence[GeofenceRecord],
    *,
    settings: SettingsLocationOut,
    min_accuracy_m: int | None,
    home_wifi_bssid: str | None = None,
) -> bool | None:
    """Return inside/outside for a history row, or None when the row is unusable."""
    if min_accuracy_m is None:
        return geofence.geofence_id in geofence_ids_containing_location(row, [geofence])
    geofence_list = list(geofences)
    if wifi_home_presence_applies(
        settings,
        geofence.geofence_id,
        row.connection_type,
        accuracy_m=row.accuracy_m,
        geofences=geofence_list,
        lat=row.lat,
        lon=row.lon,
        min_accuracy_m=min_accuracy_m,
        home_wifi_bssid=home_wifi_bssid,
        observed_wifi_bssid=row.wifi_bssid,
    ):
        return True
    if row.accuracy_m is not None and row.accuracy_m > min_accuracy_m:
        return None
    return geofence.geofence_id in geofence_ids_containing_location(row, [geofence])


def home_geofence_ids(
    settings: SettingsLocationOut,
    geofences: Sequence[GeofencePresenceTarget],
) -> frozenset[str]:
    """Return geofence ids that represent home for presence and vacation disarm.

    Resolution order: explicit ``wifi_home_geofence_id`` when it names an enabled
    geofence; otherwise enabled geofences whose radius contains settings home
    lat/lon. Empty when home is unconfigured or no geofence matches.
    """
    enabled = [row for row in geofences if row.enabled]
    if not enabled:
        return frozenset()
    explicit = (settings.wifi_home_geofence_id or "").strip()
    if explicit:
        if any(row.geofence_id == explicit for row in enabled):
            return frozenset({explicit})
        return frozenset()
    if not settings.home_configured:
        return frozenset()
    return _geofence_ids_containing_point(settings.lat, settings.lon, enabled)


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
    return home_geofence_ids(settings, geofences)


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
    home_wifi_bssid: str | None = None,
    observed_wifi_bssid: str | None = None,
) -> bool:
    """Return whether a WiFi reading should be treated as inside ``geofence_id``.

    When ``home_wifi_bssid`` is configured, match on normalized BSSID only (no
    geofence-radius fallback). Otherwise use low-accuracy ``conn=w`` plus proximity
    slack from the geofence center.
    """
    if geofence_id not in wifi_home_geofence_ids(settings, geofences):
        return False
    normalized_home_bssid = normalize_wifi_bssid(home_wifi_bssid)
    if normalized_home_bssid is not None:
        if not connection_type_is_wifi(connection_type):
            return False
        return wifi_bssids_match(observed_wifi_bssid, normalized_home_bssid)
    if not connection_type_is_wifi(connection_type):
        return False
    if not location_accuracy_is_low(accuracy_m, min_accuracy_m):
        return False
    for geofence in geofences:
        if geofence.geofence_id != geofence_id or not geofence.enabled:
            continue
        return location_within_wifi_home_proximity(lat, lon, geofence)
    return False
