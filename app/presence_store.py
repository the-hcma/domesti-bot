"""Persist user locations synced from My Tracks."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import RuleUserLastLocation, RuleUserLocationHistory
from app.db.session import discovery_session
from app.location_history_retention import (
    LocationHistoryRetention,
    retained_history_row_ids,
)
from app.logging_config import format_log_timestamp
from app.rules_store import GeofenceRecord

_LOCATION_LOGGER = logging.getLogger("location")


@dataclass(frozen=True)
class UserLocationRecord:
    accuracy_m: int | None
    lat: float
    lon: float
    received_at: float
    source: str | None
    user_id: str


def count_user_location_history(path: Path, user_id: str) -> int:
    """Return how many history rows are stored for ``user_id``."""
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleUserLocationHistory.id).where(
                RuleUserLocationHistory.user_id == user_id
            )
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


def list_user_locations(path: Path) -> dict[str, UserLocationRecord]:
    """Return the latest location per user id."""
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleUserLastLocation)).all()
        return {row.user_id: _location_to_record(row) for row in rows}


def parse_iso_timestamp_to_epoch(raw: str) -> float:
    """Parse an ISO-8601 timestamp from My Tracks export JSON."""
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError as exc:
        raise ValueError(f"Expected ISO-8601 timestamp, got {raw!r}") from exc


def prune_all_user_location_history(
    path: Path,
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Apply ``retention`` to every user and return rows deleted."""
    with discovery_session(path) as session:
        user_ids = session.scalars(
            select(RuleUserLocationHistory.user_id).distinct()
        ).all()
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
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleUserLocationHistory)
            .where(RuleUserLocationHistory.user_id == user_id)
            .order_by(
                RuleUserLocationHistory.received_at.desc(),
                RuleUserLocationHistory.id.desc(),
            )
        ).all()
        if not rows:
            return 0
        keep_ids = retained_history_row_ids(
            [(row.id, row.received_at) for row in rows],
            now=now,
            retention=retention,
        )
        delete_ids = [row.id for row in rows if row.id not in keep_ids]
        if not delete_ids:
            return 0
        session.execute(
            delete(RuleUserLocationHistory).where(
                RuleUserLocationHistory.id.in_(delete_ids)
            )
        )
        if _LOCATION_LOGGER.isEnabledFor(logging.INFO):
            _LOCATION_LOGGER.info(
                "pruned %d history row(s) for %s (kept %d)",
                len(delete_ids),
                user_id,
                len(keep_ids),
            )
        return len(delete_ids)


def replace_user_locations(
    path: Path,
    locations: list[UserLocationRecord],
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Replace all stored user locations with ``locations`` and append history."""
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(RuleUserLastLocation))
        for location in locations:
            session.add(
                RuleUserLastLocation(
                    user_id=location.user_id,
                    lat=location.lat,
                    lon=location.lon,
                    accuracy_m=location.accuracy_m,
                    received_at=location.received_at,
                    source=location.source,
                    updated_at=now,
                )
            )
            session.add(
                RuleUserLocationHistory(
                    user_id=location.user_id,
                    lat=location.lat,
                    lon=location.lon,
                    accuracy_m=location.accuracy_m,
                    received_at=location.received_at,
                    source=location.source,
                    updated_at=now,
                )
            )
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
    now = time.time()
    stored = False
    with discovery_session(path) as session:
        row = session.get(RuleUserLastLocation, location.user_id)
        if row is not None and row.received_at > location.received_at:
            return False
        if row is None:
            session.add(
                RuleUserLastLocation(
                    user_id=location.user_id,
                    lat=location.lat,
                    lon=location.lon,
                    accuracy_m=location.accuracy_m,
                    received_at=location.received_at,
                    source=location.source,
                    updated_at=now,
                )
            )
        else:
            row.lat = location.lat
            row.lon = location.lon
            row.accuracy_m = location.accuracy_m
            row.received_at = location.received_at
            row.source = location.source
            row.updated_at = now
        session.add(
            RuleUserLocationHistory(
                user_id=location.user_id,
                lat=location.lat,
                lon=location.lon,
                accuracy_m=location.accuracy_m,
                received_at=location.received_at,
                source=location.source,
                updated_at=now,
            )
        )
        stored = True
    if stored:
        _LOCATION_LOGGER.info(
            "stored location for %s (%.5f, %.5f) at %s",
            location.user_id,
            location.lat,
            location.lon,
            format_log_timestamp(location.received_at),
        )
        prune_user_location_history(
            path,
            location.user_id,
            retention=retention,
        )
    return stored


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _location_to_record(row: RuleUserLastLocation) -> UserLocationRecord:
    return UserLocationRecord(
        user_id=row.user_id,
        lat=row.lat,
        lon=row.lon,
        accuracy_m=row.accuracy_m,
        received_at=row.received_at,
        source=row.source,
    )
