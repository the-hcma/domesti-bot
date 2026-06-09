"""HTTP routes for My Tracks settings and roster/geofence sync."""

from __future__ import annotations

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
from app.db.secrets import SecretsConfigurationError
from app.mytracks_service import (
    MyTracksSyncError,
    normalize_mytracks_base_url,
    sync_geofences_from_my_tracks,
    sync_participants_from_my_tracks,
)
from app.mytracks_store import (
    MyTracksConfigRecord,
    MyTracksConfigSave,
    delete_mytracks_settings,
    load_mytracks_config,
    record_mytracks_geofences_sync,
    record_mytracks_participants_sync,
    resolve_mytracks_password,
    save_mytracks_config,
)

settings_router = APIRouter(prefix="/v1/settings", tags=["settings"])
rules_router = APIRouter(prefix="/v1/rules", tags=["rules"])


@settings_router.delete("/my-tracks", status_code=HTTPStatus.NO_CONTENT)
async def delete_mytracks_settings_route(request: Request) -> None:
    """Remove stored My Tracks settings and the encrypted admin password."""
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
    """Persist My Tracks settings and optionally update the encrypted password."""
    cache_path = _require_discovery_cache(request)
    _validate_mytracks_body(body)
    try:
        saved = save_mytracks_config(
            cache_path,
            MyTracksConfigSave(
                domain=body.domain,
                password=body.password,
                username=body.username,
            ),
        )
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
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
    if record is None:
        return MyTracksGeofencesSyncOut(geofence_count=0, last_synced_at=None)
    return MyTracksGeofencesSyncOut(
        geofence_count=0,
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
    if record is None:
        return MyTracksParticipantsSyncOut(
            last_synced_at=None,
            participant_count=0,
        )
    return MyTracksParticipantsSyncOut(
        last_synced_at=record.last_participants_sync_at,
        participant_count=0,
    )


@rules_router.post("/geofences/sync", response_model=MyTracksGeofencesSyncOut)
async def post_mytracks_geofences_sync(
    body: MyTracksSyncIn, request: Request
) -> MyTracksGeofencesSyncOut:
    """Pull geofence definitions from My Tracks using admin credentials."""
    cache_path = _require_discovery_cache(request)
    record, username, password = _resolve_sync_credentials(request, body)
    base_url = normalize_mytracks_base_url(record.domain)
    try:
        count = sync_geofences_from_my_tracks(
            base_url=base_url,
            password=password,
            username=username,
        )
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    updated = record_mytracks_geofences_sync(cache_path, count=count)
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
    record, username, password = _resolve_sync_credentials(request, body)
    base_url = normalize_mytracks_base_url(record.domain)
    try:
        count = sync_participants_from_my_tracks(
            base_url=base_url,
            password=password,
            username=username,
        )
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    updated = record_mytracks_participants_sync(cache_path, count=count)
    return MyTracksParticipantsSyncOut(
        last_synced_at=updated.last_participants_sync_at,
        participant_count=count,
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
) -> tuple[MyTracksConfigRecord, str, str]:
    record = _require_mytracks_config(request)
    cache_path = _require_discovery_cache(request)
    username = (body.username or record.username).strip()
    if username == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks admin username, got empty value",
        )
    password = resolve_mytracks_password(cache_path, draft_password=body.password)
    if password == "":
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected My Tracks admin password, got empty value",
        )
    return record, username, password


def _settings_to_schema(record: MyTracksConfigRecord) -> MyTracksSettingsOut:
    return MyTracksSettingsOut(
        domain=record.domain,
        password_configured=record.password_configured,
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
