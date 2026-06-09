"""Persist Automations participants and geofences in the discovery SQLite database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import RuleGeofence, RuleParticipant
from app.db.session import discovery_session


@dataclass(frozen=True)
class GeofenceRecord:
    center_lat: float
    center_lon: float
    enabled: bool
    geofence_id: str
    label: str
    owntracks_rid: str | None
    radius_m: int


@dataclass(frozen=True)
class ParticipantRecord:
    display_name: str
    enabled: bool
    participant_id: str
    tracking_device_label: str


def count_geofences(path: Path) -> int:
    with discovery_session(path) as session:
        return len(session.scalars(select(RuleGeofence.geofence_id)).all())


def count_participants(path: Path) -> int:
    with discovery_session(path) as session:
        return len(session.scalars(select(RuleParticipant.participant_id)).all())


def delete_geofence(path: Path, geofence_id: str) -> None:
    with discovery_session(path) as session:
        row = session.get(RuleGeofence, geofence_id)
        if row is not None:
            session.delete(row)


def list_geofences(path: Path) -> list[GeofenceRecord]:
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleGeofence).order_by(RuleGeofence.label)).all()
        return [_geofence_to_record(row) for row in rows]


def list_participants(path: Path) -> list[ParticipantRecord]:
    with discovery_session(path) as session:
        rows = session.scalars(
            select(RuleParticipant).order_by(RuleParticipant.display_name)
        ).all()
        return [_participant_to_record(row) for row in rows]


def replace_geofences(path: Path, geofences: list[GeofenceRecord]) -> int:
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(RuleGeofence))
        for geofence in geofences:
            session.add(
                RuleGeofence(
                    geofence_id=geofence.geofence_id,
                    label=geofence.label,
                    center_lat=geofence.center_lat,
                    center_lon=geofence.center_lon,
                    radius_m=geofence.radius_m,
                    enabled=1 if geofence.enabled else 0,
                    owntracks_rid=geofence.owntracks_rid,
                    updated_at=now,
                )
            )
    return len(geofences)


def replace_participants(path: Path, participants: list[ParticipantRecord]) -> int:
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(RuleParticipant))
        for participant in participants:
            session.add(
                RuleParticipant(
                    participant_id=participant.participant_id,
                    display_name=participant.display_name,
                    tracking_device_label=participant.tracking_device_label,
                    enabled=1 if participant.enabled else 0,
                    updated_at=now,
                )
            )
    return len(participants)


def save_geofence(path: Path, geofence: GeofenceRecord) -> GeofenceRecord:
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(RuleGeofence, geofence.geofence_id)
        if row is None:
            row = RuleGeofence(
                geofence_id=geofence.geofence_id,
                label=geofence.label,
                center_lat=geofence.center_lat,
                center_lon=geofence.center_lon,
                radius_m=geofence.radius_m,
                enabled=1 if geofence.enabled else 0,
                owntracks_rid=geofence.owntracks_rid,
                updated_at=now,
            )
            session.add(row)
        else:
            row.label = geofence.label
            row.center_lat = geofence.center_lat
            row.center_lon = geofence.center_lon
            row.radius_m = geofence.radius_m
            row.enabled = 1 if geofence.enabled else 0
            row.owntracks_rid = geofence.owntracks_rid
            row.updated_at = now
    return geofence


def _geofence_to_record(row: RuleGeofence) -> GeofenceRecord:
    return GeofenceRecord(
        geofence_id=row.geofence_id,
        label=row.label,
        center_lat=row.center_lat,
        center_lon=row.center_lon,
        radius_m=row.radius_m,
        enabled=bool(row.enabled),
        owntracks_rid=row.owntracks_rid,
    )


def _participant_to_record(row: RuleParticipant) -> ParticipantRecord:
    return ParticipantRecord(
        participant_id=row.participant_id,
        display_name=row.display_name,
        tracking_device_label=row.tracking_device_label,
        enabled=bool(row.enabled),
    )
