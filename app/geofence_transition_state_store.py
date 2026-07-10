"""Persist per-user geofence transition state in SQLite."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import RuleUserGeofenceState
from app.db.session import discovery_session, discovery_write


@dataclass(frozen=True)
class GeofenceTransitionStateRecord:
    geofence_id: str
    inside_since: float | None
    last_location_received_at: float | None
    outside_since: float | None
    updated_at: float
    user_id: str
    was_inside: bool


def list_geofence_transition_states(
    path: Path,
) -> dict[tuple[str, str], GeofenceTransitionStateRecord]:
    """Return persisted geofence transition state keyed by ``(user_id, geofence_id)``."""
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleUserGeofenceState)).all()
        return {
            (row.user_id, row.geofence_id): GeofenceTransitionStateRecord(
                geofence_id=row.geofence_id,
                inside_since=row.inside_since,
                last_location_received_at=row.last_location_received_at,
                outside_since=row.outside_since,
                updated_at=row.updated_at,
                user_id=row.user_id,
                was_inside=bool(row.was_inside),
            )
            for row in rows
        }


def upsert_geofence_transition_state(
    path: Path,
    *,
    geofence_id: str,
    inside_since: float | None,
    last_location_received_at: float | None,
    outside_since: float | None,
    user_id: str,
    was_inside: bool,
) -> None:
    """Upsert transition state for one ``(user_id, geofence_id)`` pair."""
    now = time.time()

    def _write(session: Session) -> None:
        row = session.get(
            RuleUserGeofenceState,
            {"user_id": user_id, "geofence_id": geofence_id},
        )
        if row is None:
            session.add(
                RuleUserGeofenceState(
                    geofence_id=geofence_id,
                    inside_since=inside_since,
                    last_location_received_at=last_location_received_at,
                    outside_since=outside_since,
                    updated_at=now,
                    user_id=user_id,
                    was_inside=1 if was_inside else 0,
                )
            )
            return
        row.inside_since = inside_since
        row.last_location_received_at = last_location_received_at
        row.outside_since = outside_since
        row.updated_at = now
        row.was_inside = 1 if was_inside else 0

    discovery_write(path, _write)
