"""Persist My Tracks connection settings in the discovery SQLite database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import MyTracksSettings
from app.db.session import discovery_session

_MYTRACKS_SETTINGS_ID = 1


@dataclass(frozen=True)
class MyTracksConfigRecord:
    domain: str
    last_geofences_sync_at: str | None
    last_participants_sync_at: str | None
    username: str


@dataclass(frozen=True)
class MyTracksConfigSave:
    domain: str
    username: str


def delete_mytracks_settings(path: Path) -> None:
    """Remove My Tracks settings."""
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is not None:
            session.delete(row)


def load_mytracks_config(path: Path) -> MyTracksConfigRecord | None:
    """Return stored My Tracks settings."""
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            return None
        return MyTracksConfigRecord(
            domain=row.domain,
            last_geofences_sync_at=_iso_from_epoch(row.last_geofences_sync_at),
            last_participants_sync_at=_iso_from_epoch(row.last_participants_sync_at),
            username=row.username,
        )


def record_mytracks_geofences_sync(path: Path, *, count: int) -> MyTracksConfigRecord:
    """Persist geofence sync metadata and return the updated settings row."""
    _ = count
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            raise RuntimeError("Expected My Tracks settings before geofence sync, got None")
        row.last_geofences_sync_at = now
        row.updated_at = now
    saved = load_mytracks_config(path)
    if saved is None:
        raise RuntimeError("Expected My Tracks settings after geofence sync, got None")
    return saved


def record_mytracks_participants_sync(path: Path, *, count: int) -> MyTracksConfigRecord:
    """Persist participant sync metadata and return the updated settings row."""
    _ = count
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            raise RuntimeError("Expected My Tracks settings before participant sync, got None")
        row.last_participants_sync_at = now
        row.updated_at = now
    saved = load_mytracks_config(path)
    if saved is None:
        raise RuntimeError("Expected My Tracks settings after participant sync, got None")
    return saved


def save_mytracks_config(path: Path, config: MyTracksConfigSave) -> MyTracksConfigRecord:
    """Upsert My Tracks domain and default admin username."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            row = MyTracksSettings(
                id=_MYTRACKS_SETTINGS_ID,
                domain=config.domain.strip(),
                username=config.username.strip(),
                last_geofences_sync_at=None,
                last_participants_sync_at=None,
                updated_at=now,
            )
            session.add(row)
        else:
            row.domain = config.domain.strip()
            row.username = config.username.strip()
            row.updated_at = now
    saved = load_mytracks_config(path)
    if saved is None:
        raise RuntimeError("Expected My Tracks settings after save, got None")
    return saved


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()
