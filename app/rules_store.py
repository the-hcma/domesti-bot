"""Persist Automations users and geofences in the discovery SQLite database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select

from app.db.models import RuleGeofence, RuleUser
from app.db.session import discovery_session
from app.user_names import default_display_name, parse_person_name


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
class UserRecord:
    display_name: str
    enabled: bool
    first_name: str
    last_name: str
    tracking_device_label: str
    user_id: str


def count_geofences(path: Path) -> int:
    with discovery_session(path) as session:
        return len(session.scalars(select(RuleGeofence.geofence_id)).all())


def count_users(path: Path) -> int:
    with discovery_session(path) as session:
        return len(session.scalars(select(RuleUser.user_id)).all())


def delete_geofence(path: Path, geofence_id: str) -> None:
    with discovery_session(path) as session:
        row = session.get(RuleGeofence, geofence_id)
        if row is not None:
            session.delete(row)


def list_geofences(path: Path) -> list[GeofenceRecord]:
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleGeofence).order_by(RuleGeofence.label)).all()
        return [_geofence_to_record(row) for row in rows]


def list_users(path: Path) -> list[UserRecord]:
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleUser).order_by(RuleUser.display_name)).all()
        return [_user_to_record(row) for row in rows]


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


def replace_users(path: Path, users: list[UserRecord]) -> int:
    """Replace the full user roster, preserving ``user_id`` values from the export."""
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(RuleUser))
        for user in users:
            session.add(
                RuleUser(
                    user_id=user.user_id,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    display_name=user.display_name,
                    tracking_device_label=user.tracking_device_label,
                    enabled=1 if user.enabled else 0,
                    updated_at=now,
                )
            )
    return len(users)


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


def user_exists(path: Path, user_id: str) -> bool:
    """True when ``user_id`` is present in the automation roster."""
    with discovery_session(path) as session:
        row = session.get(RuleUser, user_id.strip())
        return row is not None


def user_record_from_export(
    *,
    user_id: str,
    export_display_name: str,
    tracking_device_label: str,
    enabled: bool,
) -> UserRecord:
    """Build a roster row from a My Tracks users-with-devices export."""
    first_name, last_name = parse_person_name(export_display_name)
    if first_name == "":
        first_name = user_id
    return UserRecord(
        user_id=user_id,
        first_name=first_name,
        last_name=last_name,
        display_name=default_display_name(first_name),
        tracking_device_label=tracking_device_label,
        enabled=enabled,
    )


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


def _user_to_record(row: RuleUser) -> UserRecord:
    return UserRecord(
        user_id=row.user_id,
        first_name=row.first_name,
        last_name=row.last_name,
        display_name=row.display_name,
        tracking_device_label=row.tracking_device_label,
        enabled=bool(row.enabled),
    )
