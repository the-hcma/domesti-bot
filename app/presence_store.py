"""Persist participant location fixes synced from My Tracks."""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import RuleParticipantLastFix, RuleParticipantLocationHistory
from app.db.session import discovery_session
from app.location_history_retention import (
    LocationHistoryRetention,
    retained_history_row_ids,
)
from app.logging_config import format_log_timestamp
from app.rules_store import GeofenceRecord

_LOCATION_LOGGER = logging.getLogger("location")


@dataclass(frozen=True)
class ParticipantFixRecord:
    accuracy_m: int | None
    lat: float
    lon: float
    participant_id: str
    received_at: float
    source: str | None


def count_participant_location_history(path: Path, participant_id: str) -> int:
    """Return how many history rows are stored for ``participant_id``."""
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleParticipantLocationHistory.id).where(
                RuleParticipantLocationHistory.participant_id == participant_id
            )
        ).all()
        return len(rows)


def geofence_ids_containing_fix(
    fix: ParticipantFixRecord,
    geofences: list[GeofenceRecord],
) -> list[str]:
    """Return enabled geofence ids whose radius contains ``fix``."""
    inside: list[str] = []
    for geofence in geofences:
        if not geofence.enabled:
            continue
        distance_m = _haversine_m(
            fix.lat,
            fix.lon,
            geofence.center_lat,
            geofence.center_lon,
        )
        if distance_m <= geofence.radius_m:
            inside.append(geofence.geofence_id)
    return inside


def list_participant_fixes(path: Path) -> dict[str, ParticipantFixRecord]:
    """Return the latest fix per participant id."""
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleParticipantLastFix)).all()
        return {row.participant_id: _fix_to_record(row) for row in rows}


def parse_iso_timestamp_to_epoch(raw: str) -> float:
    """Parse an ISO-8601 timestamp from My Tracks export JSON."""
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError as exc:
        raise ValueError(f"Expected ISO-8601 timestamp, got {raw!r}") from exc


def prune_all_participant_location_history(
    path: Path,
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Apply ``retention`` to every participant and return rows deleted."""
    with discovery_session(path) as session:
        participant_ids = session.scalars(
            select(RuleParticipantLocationHistory.participant_id).distinct()
        ).all()
    deleted = 0
    for participant_id in participant_ids:
        deleted += prune_participant_location_history(
            path,
            participant_id,
            retention=retention,
        )
    return deleted


def prune_participant_location_history(
    path: Path,
    participant_id: str,
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Delete history rows for ``participant_id`` outside ``retention``."""
    now = time.time()
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleParticipantLocationHistory)
            .where(RuleParticipantLocationHistory.participant_id == participant_id)
            .order_by(
                RuleParticipantLocationHistory.received_at.desc(),
                RuleParticipantLocationHistory.id.desc(),
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
            delete(RuleParticipantLocationHistory).where(
                RuleParticipantLocationHistory.id.in_(delete_ids)
            )
        )
        if _LOCATION_LOGGER.isEnabledFor(logging.INFO):
            _LOCATION_LOGGER.info(
                "pruned %d history row(s) for %s (kept %d)",
                len(delete_ids),
                participant_id,
                len(keep_ids),
            )
        return len(delete_ids)


def replace_participant_fixes(
    path: Path,
    fixes: list[ParticipantFixRecord],
    *,
    retention: LocationHistoryRetention,
) -> int:
    """Replace all stored participant fixes with ``fixes`` and append history."""
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(RuleParticipantLastFix))
        for fix in fixes:
            session.add(
                RuleParticipantLastFix(
                    participant_id=fix.participant_id,
                    lat=fix.lat,
                    lon=fix.lon,
                    accuracy_m=fix.accuracy_m,
                    received_at=fix.received_at,
                    source=fix.source,
                    updated_at=now,
                )
            )
            session.add(
                RuleParticipantLocationHistory(
                    participant_id=fix.participant_id,
                    lat=fix.lat,
                    lon=fix.lon,
                    accuracy_m=fix.accuracy_m,
                    received_at=fix.received_at,
                    source=fix.source,
                    updated_at=now,
                )
            )
    for fix in fixes:
        prune_participant_location_history(
            path,
            fix.participant_id,
            retention=retention,
        )
    return len(fixes)


def upsert_participant_fix(
    path: Path,
    fix: ParticipantFixRecord,
    *,
    retention: LocationHistoryRetention,
) -> bool:
    """Upsert one participant fix and append history; return False when stale."""
    now = time.time()
    stored = False
    with discovery_session(path) as session:
        row = session.get(RuleParticipantLastFix, fix.participant_id)
        if row is not None and row.received_at > fix.received_at:
            return False
        if row is None:
            session.add(
                RuleParticipantLastFix(
                    participant_id=fix.participant_id,
                    lat=fix.lat,
                    lon=fix.lon,
                    accuracy_m=fix.accuracy_m,
                    received_at=fix.received_at,
                    source=fix.source,
                    updated_at=now,
                )
            )
        else:
            row.lat = fix.lat
            row.lon = fix.lon
            row.accuracy_m = fix.accuracy_m
            row.received_at = fix.received_at
            row.source = fix.source
            row.updated_at = now
        session.add(
            RuleParticipantLocationHistory(
                participant_id=fix.participant_id,
                lat=fix.lat,
                lon=fix.lon,
                accuracy_m=fix.accuracy_m,
                received_at=fix.received_at,
                source=fix.source,
                updated_at=now,
            )
        )
        stored = True
    if stored:
        _LOCATION_LOGGER.info(
            "stored fix for %s (%.5f, %.5f) at %s",
            fix.participant_id,
            fix.lat,
            fix.lon,
            format_log_timestamp(fix.received_at),
        )
        prune_participant_location_history(
            path,
            fix.participant_id,
            retention=retention,
        )
    return stored


def _fix_to_record(row: RuleParticipantLastFix) -> ParticipantFixRecord:
    return ParticipantFixRecord(
        participant_id=row.participant_id,
        lat=row.lat,
        lon=row.lon,
        accuracy_m=row.accuracy_m,
        received_at=row.received_at,
        source=row.source,
    )


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
