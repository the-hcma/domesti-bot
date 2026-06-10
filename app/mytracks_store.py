"""Persist My Tracks connection settings in the discovery SQLite database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.db.models import MyTracksSettings
from app.db.secrets import delete_app_secret, mytracks_relay_api_key_stored_in_db
from app.db.session import discovery_session
from app.location_history_retention import (
    DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
    DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
    LocationHistoryRetention,
    default_location_history_retention,
    location_history_retention_from_settings,
)

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


@dataclass(frozen=True)
class LocationHistoryRetentionRecord:
    max_age_hours: float
    min_keep_count: int
    unlimited: bool


@dataclass(frozen=True)
class MyTracksPairStatusRecord:
    domain: str
    domesti_public_base_url: str | None
    last_pair_error: str | None
    last_verify_at: str | None
    last_verify_ok: bool | None
    location_history_retention: LocationHistoryRetentionRecord
    location_updates_accepted: bool
    paired_at: str | None
    participant_location_test_url: str | None
    participant_location_update_url: str | None
    relay_key_configured: bool
    username: str


@dataclass(frozen=True)
class MyTracksPairingSave:
    domain: str
    domesti_public_base_url: str
    participant_location_test_url: str
    participant_location_update_url: str
    username: str


def clear_mytracks_pairing(path: Path) -> None:
    """Clear pairing metadata and delete the stored relay API key."""
    delete_app_secret(path, key="mytracks_relay_api_key")
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            return
        row.domesti_public_base_url = None
        row.last_pair_error = None
        row.paired_at = None
        row.participant_location_test_url = None
        row.participant_location_update_url = None
        row.updated_at = now


def delete_mytracks_settings(path: Path) -> None:
    """Remove My Tracks settings."""
    clear_mytracks_pairing(path)
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


def load_location_history_retention(path: Path) -> LocationHistoryRetention:
    """Return the effective location-history retention policy."""
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            return default_location_history_retention()
        return location_history_retention_from_settings(
            max_age_s=row.location_history_max_age_s,
            min_keep_count=row.location_history_min_keep_count,
            unlimited=row.location_history_unlimited,
        )


def load_mytracks_pair_status(path: Path) -> MyTracksPairStatusRecord | None:
    """Return pairing metadata for My Tracks integration."""
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            return None
        return MyTracksPairStatusRecord(
            domain=row.domain,
            domesti_public_base_url=row.domesti_public_base_url,
            last_pair_error=row.last_pair_error,
            last_verify_at=_iso_from_epoch(row.last_verify_at),
            last_verify_ok=_bool_from_int(row.last_verify_ok),
            location_history_retention=_retention_record_from_row(row),
            location_updates_accepted=bool(row.location_updates_accepted),
            paired_at=_iso_from_epoch(row.paired_at),
            participant_location_test_url=row.participant_location_test_url,
            participant_location_update_url=row.participant_location_update_url,
            relay_key_configured=mytracks_relay_api_key_stored_in_db(path),
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
                location_history_max_age_s=DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
                location_history_min_keep_count=DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
                location_history_unlimited=0,
                location_updates_accepted=1,
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


def save_location_history_retention(
    path: Path,
    *,
    max_age_hours: float,
    min_keep_count: int,
    unlimited: bool,
) -> LocationHistoryRetentionRecord:
    """Persist location-history retention settings."""
    now = time.time()
    max_age_s = max_age_hours * 3600.0
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            row = MyTracksSettings(
                id=_MYTRACKS_SETTINGS_ID,
                domain="",
                username="",
                last_geofences_sync_at=None,
                last_participants_sync_at=None,
                location_history_max_age_s=max_age_s,
                location_history_min_keep_count=min_keep_count,
                location_history_unlimited=1 if unlimited else 0,
                location_updates_accepted=1,
                updated_at=now,
            )
            session.add(row)
        else:
            row.location_history_max_age_s = max_age_s
            row.location_history_min_keep_count = min_keep_count
            row.location_history_unlimited = 1 if unlimited else 0
            row.updated_at = now
    return _retention_record_from_row(row)


def save_mytracks_pairing(path: Path, pairing: MyTracksPairingSave) -> MyTracksPairStatusRecord:
    """Persist successful pairing metadata."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            row = MyTracksSettings(
                id=_MYTRACKS_SETTINGS_ID,
                domain=pairing.domain.strip(),
                username=pairing.username.strip(),
                domesti_public_base_url=pairing.domesti_public_base_url.strip(),
                participant_location_update_url=pairing.participant_location_update_url.strip(),
                participant_location_test_url=pairing.participant_location_test_url.strip(),
                last_geofences_sync_at=None,
                last_participants_sync_at=None,
                location_history_max_age_s=DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
                location_history_min_keep_count=DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
                location_history_unlimited=0,
                location_updates_accepted=1,
                paired_at=now,
                last_pair_error=None,
                updated_at=now,
            )
            session.add(row)
        else:
            row.domain = pairing.domain.strip()
            row.username = pairing.username.strip()
            row.domesti_public_base_url = pairing.domesti_public_base_url.strip()
            row.participant_location_update_url = (
                pairing.participant_location_update_url.strip()
            )
            row.participant_location_test_url = pairing.participant_location_test_url.strip()
            row.paired_at = now
            row.last_pair_error = None
            row.location_updates_accepted = 1
            row.updated_at = now
    saved = load_mytracks_pair_status(path)
    if saved is None:
        raise RuntimeError("Expected My Tracks settings after pairing, got None")
    return saved


def set_last_pair_error(path: Path, error: str | None) -> None:
    """Record the latest pairing failure message."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            return
        row.last_pair_error = error
        row.updated_at = now


def set_location_updates_accepted(path: Path, *, accepted: bool) -> MyTracksPairStatusRecord:
    """Update the local emergency switch for live location-update ingest."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(MyTracksSettings, _MYTRACKS_SETTINGS_ID)
        if row is None:
            raise RuntimeError("Expected My Tracks settings before location toggle, got None")
        row.location_updates_accepted = 1 if accepted else 0
        row.updated_at = now
    saved = load_mytracks_pair_status(path)
    if saved is None:
        raise RuntimeError("Expected My Tracks settings after location toggle, got None")
    return saved


def _retention_record_from_row(row: MyTracksSettings) -> LocationHistoryRetentionRecord:
    max_age_s = (
        row.location_history_max_age_s
        if row.location_history_max_age_s is not None
        else DEFAULT_LOCATION_HISTORY_MAX_AGE_S
    )
    min_keep_count = (
        row.location_history_min_keep_count
        if row.location_history_min_keep_count is not None
        else DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT
    )
    return LocationHistoryRetentionRecord(
        max_age_hours=max_age_s / 3600.0,
        min_keep_count=min_keep_count,
        unlimited=bool(row.location_history_unlimited),
    )


def _bool_from_int(value: int | None) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _iso_from_epoch(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC).isoformat()
