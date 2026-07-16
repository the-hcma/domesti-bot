"""Persist user locations synced from My Tracks."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import RuleUserLastLocation, RuleUserLocationHistory
from app.db.session import discovery_session, discovery_write
from app.location_history_retention import (
    LocationHistoryRetention,
    retained_history_row_ids,
)
from app.location_report import (
    location_report_log_fragment,
    location_report_log_stale_suffix,
)
from app.presence_connection_type import (
    connection_type_label_for_log,
    normalize_presence_connection_type,
)
from app.presence_wifi import normalize_wifi_bssid
from app.rules_store import GeofenceRecord

_LOCATION_LOGGER = logging.getLogger("location")


@dataclass(frozen=True)
class ObservedWifiNetwork:
    last_seen_at: float
    wifi_bssid: str
    wifi_ssid: str


@dataclass(frozen=True)
class UserLocationRecord:
    accuracy_m: int | None
    fix_at: float
    lat: float
    lon: float
    reported_at: float
    source: str | None
    user_id: str
    battery_level: int | None = None
    connection_type: str | None = None
    fix_source: str | None = None
    trigger: str | None = None
    wifi_bssid: str | None = None
    wifi_ssid: str | None = None


def count_user_location_history(path: Path, user_id: str) -> int:
    """Return how many history rows are stored for ``user_id``."""
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleUserLocationHistory.id).where(RuleUserLocationHistory.user_id == user_id)
        ).all()
        return len(rows)


def geofence_ids_containing_location(
    location: UserLocationRecord,
    geofences: list[GeofenceRecord],
) -> list[str]:
    """Return enabled geofence ids whose radius contains ``location``."""
    inside: list[str] = []
    for geofence in geofences:
        if not geofence.enabled:
            continue
        distance_m = _haversine_m(
            location.lat,
            location.lon,
            geofence.center_lat,
            geofence.center_lon,
        )
        if distance_m <= geofence.radius_m:
            inside.append(geofence.geofence_id)
    return inside


def list_observed_wifi_networks_for_user(
    path: Path,
    user_id: str,
    *,
    limit: int = 20,
) -> list[ObservedWifiNetwork]:
    """Return distinct WiFi networks seen in ``user_id`` location history."""
    if limit <= 0:
        raise ValueError(f"Expected limit > 0, got {limit}")
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleUserLocationHistory)
            .where(RuleUserLocationHistory.user_id == user_id)
            .where(RuleUserLocationHistory.wifi_bssid.is_not(None))
            .where(RuleUserLocationHistory.wifi_ssid.is_not(None))
            .order_by(
                RuleUserLocationHistory.reported_at.desc(),
                RuleUserLocationHistory.id.desc(),
            )
        ).all()
    latest_by_bssid: dict[str, ObservedWifiNetwork] = {}
    for row in rows:
        bssid = normalize_wifi_bssid(row.wifi_bssid)
        ssid = (row.wifi_ssid or "").strip()
        if bssid is None or ssid == "":
            continue
        if bssid in latest_by_bssid:
            continue
        latest_by_bssid[bssid] = ObservedWifiNetwork(
            wifi_ssid=ssid,
            wifi_bssid=bssid,
            last_seen_at=row.reported_at,
        )
        if len(latest_by_bssid) >= limit:
            break
    return sorted(
        latest_by_bssid.values(),
        key=lambda network: network.last_seen_at,
        reverse=True,
    )


def list_user_location_history_for_user(
    path: Path,
    user_id: str,
    *,
    since: float | None = None,
) -> list[UserLocationRecord]:
    """Return location history for ``user_id`` oldest-first.

    When ``since`` is set, only rows with ``reported_at >= since`` are returned.
    """
    with discovery_session(path) as session:
        query = select(RuleUserLocationHistory).where(RuleUserLocationHistory.user_id == user_id)
        if since is not None:
            query = query.where(RuleUserLocationHistory.reported_at >= since)
        rows = session.scalars(
            query.order_by(
                RuleUserLocationHistory.reported_at.asc(),
                RuleUserLocationHistory.id.asc(),
            )
        ).all()
        return [_history_to_record(row) for row in rows]


def list_user_location_history_for_walkback(
    path: Path,
    user_id: str,
    *,
    now_epoch: float,
    walkback_max_s: float,
    limit: int | None = None,
) -> list[UserLocationRecord]:
    """Return newest-first history within a walkback window.

    When ``limit`` is set, cap rows per user; otherwise return every row in the
    window bounded only by ``walkback_max_s``.
    """
    rows_by_user = list_user_location_history_for_walkback_by_user(
        path,
        {user_id},
        now_epoch=now_epoch,
        walkback_max_s=walkback_max_s,
        limit_per_user=limit,
    )
    return rows_by_user.get(user_id, [])


def list_user_location_history_for_walkback_by_user(
    path: Path,
    user_ids: set[str] | list[str],
    *,
    now_epoch: float,
    walkback_max_s: float,
    limit_per_user: int | None = None,
) -> dict[str, list[UserLocationRecord]]:
    """Return newest-first walkback history for each user in one SQLite session."""
    if walkback_max_s <= 0:
        raise ValueError(
            f"Expected walkback_max_s > 0, got {walkback_max_s}",
        )
    if limit_per_user is not None and limit_per_user <= 0:
        raise ValueError(f"Expected limit_per_user > 0, got {limit_per_user}")
    unique_user_ids = set(user_ids)
    if not unique_user_ids:
        return {}
    since = now_epoch - walkback_max_s
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleUserLocationHistory)
            .where(RuleUserLocationHistory.user_id.in_(unique_user_ids))
            .where(RuleUserLocationHistory.reported_at >= since)
            .order_by(
                RuleUserLocationHistory.reported_at.desc(),
                RuleUserLocationHistory.id.desc(),
            )
        ).all()
    history_by_user: dict[str, list[UserLocationRecord]] = {user_id: [] for user_id in unique_user_ids}
    for row in rows:
        bucket = history_by_user.get(row.user_id)
        if bucket is None:
            continue
        if limit_per_user is not None and len(bucket) >= limit_per_user:
            continue
        bucket.append(_history_to_record(row))
    return history_by_user


def list_user_locations(path: Path) -> dict[str, UserLocationRecord]:
    """Return the latest location per user id."""
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleUserLastLocation)).all()
        return {row.user_id: _location_to_record(row) for row in rows}


def prune_all_user_location_history(
    path: Path,
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Apply ``retention`` to every user and return rows deleted."""
    with discovery_session(path) as session:
        user_ids = session.scalars(select(RuleUserLocationHistory.user_id).distinct()).all()
    deleted = 0
    for user_id in user_ids:
        deleted += prune_user_location_history(
            path,
            user_id,
            retention=retention,
        )
    return deleted


def prune_user_location_history(
    path: Path,
    user_id: str,
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Delete history rows for ``user_id`` outside ``retention``."""
    now = time.time()

    def _write(session: Session) -> int:
        rows = session.scalars(
            select(RuleUserLocationHistory)
            .where(RuleUserLocationHistory.user_id == user_id)
            .order_by(
                RuleUserLocationHistory.reported_at.desc(),
                RuleUserLocationHistory.id.desc(),
            )
        ).all()
        if not rows:
            return 0
        keep_ids = retained_history_row_ids(
            [(row.id, row.reported_at) for row in rows],
            now=now,
            retention=retention,
        )
        delete_ids = [row.id for row in rows if row.id not in keep_ids]
        if not delete_ids:
            return 0
        session.execute(delete(RuleUserLocationHistory).where(RuleUserLocationHistory.id.in_(delete_ids)))
        if _LOCATION_LOGGER.isEnabledFor(logging.INFO):
            _LOCATION_LOGGER.info(
                "pruned %d history row(s) for %s (kept %d)",
                len(delete_ids),
                user_id,
                len(keep_ids),
            )
        return len(delete_ids)

    return discovery_write(path, _write)


def replace_user_locations(
    path: Path,
    locations: list[UserLocationRecord],
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Replace all stored user locations with ``locations`` and append history."""
    now = time.time()

    def _write(session: Session) -> None:
        session.execute(delete(RuleUserLastLocation))
        for location in locations:
            stored_location = _location_with_normalized_fields(location)
            session.add(
                RuleUserLastLocation(
                    user_id=stored_location.user_id,
                    lat=stored_location.lat,
                    lon=stored_location.lon,
                    accuracy_m=stored_location.accuracy_m,
                    battery_level=stored_location.battery_level,
                    connection_type=stored_location.connection_type,
                    fix_at=stored_location.fix_at,
                    fix_source=stored_location.fix_source,
                    reported_at=stored_location.reported_at,
                    source=stored_location.source,
                    trigger=stored_location.trigger,
                    updated_at=now,
                    wifi_bssid=stored_location.wifi_bssid,
                    wifi_ssid=stored_location.wifi_ssid,
                )
            )
            session.add(
                RuleUserLocationHistory(
                    user_id=stored_location.user_id,
                    lat=stored_location.lat,
                    lon=stored_location.lon,
                    accuracy_m=stored_location.accuracy_m,
                    battery_level=stored_location.battery_level,
                    connection_type=stored_location.connection_type,
                    fix_at=stored_location.fix_at,
                    fix_source=stored_location.fix_source,
                    reported_at=stored_location.reported_at,
                    source=stored_location.source,
                    trigger=stored_location.trigger,
                    updated_at=now,
                    wifi_bssid=stored_location.wifi_bssid,
                    wifi_ssid=stored_location.wifi_ssid,
                )
            )

    discovery_write(path, _write)
    for location in locations:
        prune_user_location_history(
            path,
            location.user_id,
            retention=retention,
        )
    return len(locations)


def upsert_user_location(
    path: Path,
    location: UserLocationRecord,
    *,
    retention: LocationHistoryRetention,
) -> bool:
    """Upsert one user location and append history; return False when stale."""
    location = _location_with_normalized_fields(location)
    now = time.time()

    def _write(session: Session) -> bool:
        row = session.get(RuleUserLastLocation, location.user_id)
        if row is not None and row.reported_at > location.reported_at:
            if _LOCATION_LOGGER.isEnabledFor(logging.DEBUG):
                _LOCATION_LOGGER.debug(
                    "skipped stale location for %s%s",
                    location.user_id,
                    location_report_log_fragment(
                        reported_at=location.reported_at,
                        fix_at=location.fix_at,
                    ),
                )
            return False
        if row is None:
            session.add(
                RuleUserLastLocation(
                    user_id=location.user_id,
                    lat=location.lat,
                    lon=location.lon,
                    accuracy_m=location.accuracy_m,
                    battery_level=location.battery_level,
                    connection_type=location.connection_type,
                    fix_at=location.fix_at,
                    fix_source=location.fix_source,
                    reported_at=location.reported_at,
                    source=location.source,
                    trigger=location.trigger,
                    updated_at=now,
                    wifi_bssid=location.wifi_bssid,
                    wifi_ssid=location.wifi_ssid,
                )
            )
        else:
            row.lat = location.lat
            row.lon = location.lon
            row.accuracy_m = location.accuracy_m
            row.battery_level = location.battery_level
            row.connection_type = location.connection_type
            row.fix_at = location.fix_at
            row.fix_source = location.fix_source
            row.reported_at = location.reported_at
            row.source = location.source
            row.trigger = location.trigger
            row.updated_at = now
            row.wifi_bssid = location.wifi_bssid
            row.wifi_ssid = location.wifi_ssid
        session.add(
            RuleUserLocationHistory(
                user_id=location.user_id,
                lat=location.lat,
                lon=location.lon,
                accuracy_m=location.accuracy_m,
                battery_level=location.battery_level,
                connection_type=location.connection_type,
                fix_at=location.fix_at,
                fix_source=location.fix_source,
                reported_at=location.reported_at,
                source=location.source,
                trigger=location.trigger,
                updated_at=now,
                wifi_bssid=location.wifi_bssid,
                wifi_ssid=location.wifi_ssid,
            )
        )
        return True

    stored = discovery_write(path, _write)
    if stored:
        accuracy_label = "unknown" if location.accuracy_m is None else f"{location.accuracy_m:g}"
        connection_label = connection_type_label_for_log(location.connection_type)
        metadata_suffix = _location_log_metadata_suffix(location)
        report_fragment = location_report_log_fragment(
            reported_at=location.reported_at,
            fix_at=location.fix_at,
        )
        stale_suffix = location_report_log_stale_suffix(
            reported_at=location.reported_at,
            fix_at=location.fix_at,
            trigger=location.trigger,
        )
        _LOCATION_LOGGER.info(
            "stored location for %s (%.5f, %.5f) accuracy_m=%s connection_type=%s%s%s%s",
            location.user_id,
            location.lat,
            location.lon,
            accuracy_label,
            connection_label,
            metadata_suffix,
            report_fragment,
            stale_suffix,
        )
        prune_user_location_history(
            path,
            location.user_id,
            retention=retention,
        )
    return stored


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in metres between two WGS84 points."""
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _history_to_record(row: RuleUserLocationHistory) -> UserLocationRecord:
    """Map a ``RuleUserLocationHistory`` ORM row to ``UserLocationRecord``."""
    return UserLocationRecord(
        user_id=row.user_id,
        lat=row.lat,
        lon=row.lon,
        accuracy_m=row.accuracy_m,
        battery_level=row.battery_level,
        connection_type=row.connection_type,
        fix_at=row.fix_at,
        fix_source=row.fix_source,
        reported_at=row.reported_at,
        source=row.source,
        trigger=row.trigger,
        wifi_bssid=row.wifi_bssid,
        wifi_ssid=row.wifi_ssid,
    )


def _location_log_metadata_suffix(location: UserLocationRecord) -> str:
    parts: list[str] = []
    if location.wifi_ssid is not None:
        parts.append(f"wifi_ssid={location.wifi_ssid!r}")
    if location.wifi_bssid is not None:
        parts.append(f"wifi_bssid={location.wifi_bssid}")
    if location.fix_source is not None:
        parts.append(f"fix_source={location.fix_source!r}")
    if location.trigger is not None:
        parts.append(f"trigger={location.trigger!r}")
    if location.battery_level is not None:
        parts.append(f"battery_level={location.battery_level}")
    if not parts:
        return ""
    return " " + " ".join(parts)


def _location_to_record(row: RuleUserLastLocation) -> UserLocationRecord:
    """Map a ``RuleUserLastLocation`` ORM row to ``UserLocationRecord``."""
    return UserLocationRecord(
        user_id=row.user_id,
        lat=row.lat,
        lon=row.lon,
        accuracy_m=row.accuracy_m,
        battery_level=row.battery_level,
        connection_type=row.connection_type,
        fix_at=row.fix_at,
        fix_source=row.fix_source,
        reported_at=row.reported_at,
        source=row.source,
        trigger=row.trigger,
        wifi_bssid=row.wifi_bssid,
        wifi_ssid=row.wifi_ssid,
    )


def _location_with_normalized_fields(
    location: UserLocationRecord,
) -> UserLocationRecord:
    normalized_connection_type = normalize_presence_connection_type(
        location.connection_type,
    )
    normalized_wifi_bssid = normalize_wifi_bssid(location.wifi_bssid)
    if normalized_connection_type == location.connection_type and normalized_wifi_bssid == location.wifi_bssid:
        return location
    return replace(
        location,
        connection_type=normalized_connection_type,
        wifi_bssid=normalized_wifi_bssid,
    )
