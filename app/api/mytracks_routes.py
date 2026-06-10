"""HTTP routes for My Tracks settings and roster/geofence sync."""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    MyTracksGeofencesSyncOut,
    MyTracksParticipantsSyncOut,
    MyTracksSettingsIn,
    MyTracksSettingsOut,
    MyTracksSyncIn,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.mytracks_service import (
    ExportedParticipant,
    MyTracksSyncError,
    fetch_geofences_from_my_tracks,
    fetch_participants_from_my_tracks,
    normalize_mytracks_base_url,
)
from app.mytracks_store import (
    MyTracksConfigRecord,
    MyTracksConfigSave,
    delete_mytracks_settings,
    load_mytracks_config,
    record_mytracks_geofences_sync,
    record_mytracks_participants_sync,
    save_mytracks_config,
)
from app.presence_store import (
    ParticipantFixRecord,
    parse_iso_timestamp_to_epoch,
    replace_participant_fixes,
)
from app.rules_store import (
    GeofenceRecord,
    ParticipantRecord,
    count_geofences,
    count_participants,
    replace_geofences,
    replace_participants,
)

settings_router = APIRouter(prefix="/v1/settings", tags=["settings"])
rules_router = APIRouter(prefix="/v1/rules", tags=["rules"])

_LOGGER = logging.getLogger(__name__)


@settings_router.delete("/my-tracks", status_code=HTTPStatus.NO_CONTENT)
async def delete_mytracks_settings_route(request: Request) -> None:
    """Remove stored My Tracks settings."""
    cache_path = _require_discovery_cache(request)
    delete_mytracks_settings(cache_path)


@settings_router.get("/my-tracks", response_model=MyTracksSettingsOut | None)
async def get_mytracks_settings(request: Request) -> MyTracksSettingsOut | None:
    """Return stored My Tracks settings, or ``null`` when not configured."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return None
    record = load_mytracks_config(cache_path)
    if record is None:
        return None
    return _settings_to_schema(record)


@settings_router.put("/my-tracks", response_model=MyTracksSettingsOut)
async def put_mytracks_settings(
    body: MyTracksSettingsIn, request: Request
) -> MyTracksSettingsOut:
    """Persist My Tracks domain and default admin username."""
    cache_path = _require_discovery_cache(request)
    _validate_mytracks_body(body)
    saved = save_mytracks_config(
        cache_path,
        MyTracksConfigSave(
            domain=body.domain,
            username=body.username,
        ),
    )
    return _settings_to_schema(saved)


@rules_router.get("/geofences/sync-status", response_model=MyTracksGeofencesSyncOut)
async def get_mytracks_geofences_sync_status(
    request: Request,
) -> MyTracksGeofencesSyncOut:
    """Return last geofence sync metadata from My Tracks settings."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return MyTracksGeofencesSyncOut(geofence_count=0, last_synced_at=None)
    record = load_mytracks_config(cache_path)
    geofence_count = count_geofences(cache_path)
    if record is None:
        return MyTracksGeofencesSyncOut(geofence_count=geofence_count, last_synced_at=None)
    return MyTracksGeofencesSyncOut(
        geofence_count=geofence_count,
        last_synced_at=record.last_geofences_sync_at,
    )


@rules_router.get("/participants/sync-status", response_model=MyTracksParticipantsSyncOut)
async def get_mytracks_participants_sync_status(
    request: Request,
) -> MyTracksParticipantsSyncOut:
    """Return last participant roster sync metadata from My Tracks settings."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return MyTracksParticipantsSyncOut(
            last_synced_at=None,
            participant_count=0,
        )
    record = load_mytracks_config(cache_path)
    participant_count = count_participants(cache_path)
    if record is None:
        return MyTracksParticipantsSyncOut(
            last_synced_at=None,
            participant_count=participant_count,
        )
    return MyTracksParticipantsSyncOut(
        last_synced_at=record.last_participants_sync_at,
        participant_count=participant_count,
    )


@rules_router.post("/geofences/sync", response_model=MyTracksGeofencesSyncOut)
async def post_mytracks_geofences_sync(
    body: MyTracksSyncIn, request: Request
) -> MyTracksGeofencesSyncOut:
    """Pull geofence definitions from My Tracks using admin credentials."""
    cache_path = _require_discovery_cache(request)
    record, username = _resolve_sync_credentials(request, body)
    base_url = normalize_mytracks_base_url(record.domain)
    _LOGGER.info(
        "[mytracks] geofence sync starting for %s as %s",
        base_url,
        username,
    )
    try:
        exported = fetch_geofences_from_my_tracks(
            base_url=base_url,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError as exc:
        _LOGGER.warning(
            "[mytracks] geofence sync failed for %s as %s: %s",
            base_url,
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    count = replace_geofences(
        cache_path,
        [
            GeofenceRecord(
                geofence_id=row.geofence_id,
                label=row.label,
                center_lat=row.center_lat,
                center_lon=row.center_lon,
                radius_m=row.radius_m,
                enabled=row.enabled,
                owntracks_rid=row.owntracks_rid,
            )
            for row in exported
        ],
    )
    updated = record_mytracks_geofences_sync(cache_path, count=count)
    _LOGGER.info(
        "[mytracks] geofence sync complete for %s: %d geofence(s)",
        base_url,
        count,
    )
    return MyTracksGeofencesSyncOut(
        geofence_count=count,
        last_synced_at=updated.last_geofences_sync_at,
    )


@rules_router.post("/participants/sync", response_model=MyTracksParticipantsSyncOut)
async def post_mytracks_participants_sync(
    body: MyTracksSyncIn, request: Request
) -> MyTracksParticipantsSyncOut:
    """Pull the participant roster from My Tracks using admin credentials."""
    cache_path = _require_discovery_cache(request)
    record, username = _resolve_sync_credentials(request, body)
    base_url = normalize_mytracks_base_url(record.domain)
    _LOGGER.info(
        "[mytracks] participant sync starting for %s as %s",
        base_url,
        username,
    )
    try:
        exported = fetch_participants_from_my_tracks(
            base_url=base_url,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError as exc:
        _LOGGER.warning(
            "[mytracks] participant sync failed for %s as %s: %s",
            base_url,
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    count = replace_participants(
        cache_path,
        [
            ParticipantRecord(
                participant_id=row.participant_id,
                display_name=row.display_name,
                tracking_device_label=row.tracking_device_label,
                enabled=row.enabled,
            )
            for row in exported
        ],
    )
    fix_count = replace_participant_fixes(
        cache_path,
        [_participant_fix_from_export(row) for row in exported if row.latest_location is not None],
    )
    updated = record_mytracks_participants_sync(cache_path, count=count)
    _LOGGER.info(
        "[mytracks] participant sync complete for %s: %d participant(s), %d location fix(es)",
        base_url,
        count,
        fix_count,
    )
    return MyTracksParticipantsSyncOut(
        last_synced_at=updated.last_participants_sync_at,
        participant_count=count,
    )


def _participant_fix_from_export(row: ExportedParticipant) -> ParticipantFixRecord:
    location = row.latest_location
    if location is None:
        raise ValueError("Expected latest_location, got None")
    return ParticipantFixRecord(
        participant_id=row.participant_id,
        lat=location.lat,
        lon=location.lon,
        accuracy_m=location.accuracy_m,
        received_at=parse_iso_timestamp_to_epoch(location.received_at),
        source="my-tracks",
    )


def _require_discovery_cache(request: Request) -> Path:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist My Tracks settings: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    return cache_path


def _require_mytracks_config(request: Request) -> MyTracksConfigRecord:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail="My Tracks settings require a discovery cache path",
        )
    record = load_mytracks_config(cache_path)
    if record is None:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks settings, got unconfigured state",
        )
    return record


def _resolve_sync_credentials(
    request: Request,
    body: MyTracksSyncIn,
) -> tuple[MyTracksConfigRecord, str]:
    record = _require_mytracks_config(request)
    username = (body.username or record.username).strip()
    if username == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks admin username, got empty value",
        )
    if body.password.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks admin password, got empty value",
        )
    return record, username


def _settings_to_schema(record: MyTracksConfigRecord) -> MyTracksSettingsOut:
    return MyTracksSettingsOut(
        domain=record.domain,
        username=record.username,
    )


def _validate_mytracks_body(body: MyTracksSettingsIn) -> None:
    if body.domain.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks domain, got empty value",
        )
    if body.username.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks admin username, got empty value",
        )
    try:
        normalize_mytracks_base_url(body.domain)
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
