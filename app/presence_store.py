"""Persist participant location fixes synced from My Tracks."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import RuleParticipantLastFix
from app.db.session import discovery_session
from app.rules_store import GeofenceRecord


@dataclass(frozen=True)
class ParticipantFixRecord:
    accuracy_m: int | None
    lat: float
    lon: float
    participant_id: str
    received_at: float
    source: str | None


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


def replace_participant_fixes(path: Path, fixes: list[ParticipantFixRecord]) -> int:
    """Replace all stored participant fixes with ``fixes``."""
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
    return len(fixes)


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
