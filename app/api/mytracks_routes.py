"""HTTP routes for My Tracks settings and roster/geofence sync."""

from __future__ import annotations

import logging
import secrets
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    LocationHistoryRetentionIn,
    LocationHistoryRetentionOut,
    MyTracksGeofencesSyncOut,
    MyTracksLocationUpdatesIn,
    MyTracksLocationUpdatesOut,
    MyTracksPairIn,
    MyTracksPairStatusOut,
    MyTracksParticipantsSyncOut,
    MyTracksSettingsIn,
    MyTracksSettingsOut,
    MyTracksSyncIn,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import (
    SecretsConfigurationError,
    save_mytracks_relay_api_key_to_db,
    secrets_key_configured,
)
from app.mytracks_service import (
    ExportedParticipant,
    MyTracksSyncError,
    build_location_update_webhook_urls,
    fetch_geofences_from_my_tracks,
    fetch_mytracks_domesti_config,
    fetch_participants_from_my_tracks,
    normalize_mytracks_base_url,
    normalize_public_base_url,
    pair_with_my_tracks,
    patch_mytracks_location_updates,
)
from app.mytracks_store import (
    LocationHistoryRetentionRecord,
    MyTracksConfigRecord,
    MyTracksConfigSave,
    MyTracksPairStatusRecord,
    MyTracksPairingSave,
    delete_mytracks_settings,
    load_location_history_retention,
    load_mytracks_config,
    load_mytracks_pair_status,
    record_mytracks_geofences_sync,
    record_mytracks_participants_sync,
    save_location_history_retention,
    save_mytracks_config,
    save_mytracks_pairing,
    set_last_pair_error,
    set_location_updates_accepted,
)
from app.presence_store import (
    ParticipantFixRecord,
    parse_iso_timestamp_to_epoch,
    prune_all_participant_location_history,
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


@settings_router.get("/my-tracks/pair-status", response_model=MyTracksPairStatusOut | None)
async def get_mytracks_pair_status(request: Request) -> MyTracksPairStatusOut | None:
    """Return pairing metadata for domesti-bot ↔ my-tracks integration."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return None
    record = load_mytracks_pair_status(cache_path)
    if record is None:
        return None
    return _pair_status_to_schema(record, cache_path=cache_path)


@settings_router.patch(
    "/my-tracks/location-history-retention",
    response_model=LocationHistoryRetentionOut,
)
async def patch_mytracks_location_history_retention_route(
    body: LocationHistoryRetentionIn,
    request: Request,
) -> LocationHistoryRetentionOut:
    """Update how many location fixes are kept per participant."""
    cache_path = _require_discovery_cache(request)
    _require_mytracks_config(request)
    saved = save_location_history_retention(
        cache_path,
        max_age_hours=body.max_age_hours,
        min_keep_count=body.min_keep_count,
        unlimited=body.unlimited,
    )
    retention = load_location_history_retention(cache_path)
    pruned = prune_all_participant_location_history(cache_path, retention=retention)
    if pruned:
        _LOGGER.info(
            "[mytracks] location-history retention update pruned %d row(s)",
            pruned,
        )
    return _retention_record_to_schema(saved)


@settings_router.patch("/my-tracks/location-updates", response_model=MyTracksLocationUpdatesOut)
async def patch_mytracks_location_updates_route(
    body: MyTracksLocationUpdatesIn,
    request: Request,
) -> MyTracksLocationUpdatesOut:
    """Toggle whether domesti-bot accepts live location relays."""
    cache_path = _require_discovery_cache(request)
    record = _require_paired_mytracks(cache_path)
    updated = set_location_updates_accepted(cache_path, accepted=body.accepted)
    mytracks_enabled: bool | None = None
    if body.password is not None and body.password.strip() != "":
        base_url = normalize_mytracks_base_url(record.domain)
        try:
            patch_mytracks_location_updates(
                base_url=base_url,
                enabled=body.accepted,
                password=body.password,
                username=record.username,
            )
        except MyTracksSyncError as exc:
            raise HTTPException(
                status_code=HTTPStatus.BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        mytracks_enabled = body.accepted
    return MyTracksLocationUpdatesOut(
        accepted=updated.location_updates_accepted,
        mytracks_location_updates_enabled=mytracks_enabled,
    )


@settings_router.post("/my-tracks/pair", response_model=MyTracksPairStatusOut)
async def post_mytracks_pair(
    body: MyTracksPairIn,
    request: Request,
) -> MyTracksPairStatusOut:
    """Generate a relay secret, persist it, and register webhook URLs on my-tracks."""
    cache_path = _require_discovery_cache(request)
    _require_secrets_key_for_pairing()
    mytracks_base = _validated_mytracks_domain(body.domain)
    domesti_public = _validated_domesti_public_url(body.domesti_public_base_url)
    username = body.username.strip()
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
    update_url, test_url = build_location_update_webhook_urls(domesti_public)
    retention_input = body.location_history_retention
    save_location_history_retention(
        cache_path,
        max_age_hours=retention_input.max_age_hours,
        min_keep_count=retention_input.min_keep_count,
        unlimited=retention_input.unlimited,
    )
    relay_key = secrets.token_urlsafe(32)
    _LOGGER.info(
        "[mytracks] pairing starting for %s as %s (domesti public %s)",
        mytracks_base,
        username,
        domesti_public,
    )
    try:
        save_mytracks_relay_api_key_to_db(cache_path, relay_key)
    except SecretsConfigurationError as exc:
        _LOGGER.warning(
            "[mytracks] pairing failed for %s as %s before my-tracks call: %s",
            mytracks_base,
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    try:
        pair_status = pair_with_my_tracks(
            api_key=relay_key,
            base_url=mytracks_base,
            domesti_base_url=domesti_public,
            participant_location_update_url=update_url,
            participant_location_test_url=test_url,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError as exc:
        set_last_pair_error(cache_path, str(exc))
        _LOGGER.warning(
            "[mytracks] pairing failed for %s as %s: %s",
            mytracks_base,
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    save_mytracks_config(
        cache_path,
        MyTracksConfigSave(domain=mytracks_base, username=username),
    )
    saved = save_mytracks_pairing(
        cache_path,
        MyTracksPairingSave(
            domain=mytracks_base,
            username=username,
            domesti_public_base_url=domesti_public,
            participant_location_update_url=update_url,
            participant_location_test_url=test_url,
        ),
    )
    _LOGGER.info(
        "[mytracks] pairing complete for %s as %s (HTTP %d)",
        mytracks_base,
        username,
        pair_status,
    )
    return _pair_status_to_schema(saved, cache_path=cache_path)


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
        retention=load_location_history_retention(cache_path),
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


def _maybe_mytracks_location_updates_enabled(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None,
) -> bool | None:
    if record.paired_at is None:
        return None
    if password is None or password.strip() == "":
        return None
    base_url = normalize_mytracks_base_url(record.domain)
    try:
        config = fetch_mytracks_domesti_config(
            base_url=base_url,
            password=password,
            username=record.username,
        )
    except MyTracksSyncError:
        return None
    return config.location_updates_enabled


def _pair_status_to_schema(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None = None,
) -> MyTracksPairStatusOut:
    return MyTracksPairStatusOut(
        paired_at=record.paired_at,
        domain=record.domain,
        username=record.username,
        domesti_public_base_url=record.domesti_public_base_url,
        participant_location_update_url=record.participant_location_update_url,
        participant_location_test_url=record.participant_location_test_url,
        relay_key_configured=record.relay_key_configured,
        location_history_retention=_retention_record_to_schema(
            record.location_history_retention
        ),
        location_updates_accepted=record.location_updates_accepted,
        mytracks_location_updates_enabled=_maybe_mytracks_location_updates_enabled(
            record,
            cache_path=cache_path,
            password=password,
        ),
        last_verify_at=record.last_verify_at,
        last_verify_ok=record.last_verify_ok,
        last_pair_error=record.last_pair_error,
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


def _require_paired_mytracks(cache_path: Path) -> MyTracksPairStatusRecord:
    record = load_mytracks_pair_status(cache_path)
    if record is None or record.paired_at is None or not record.relay_key_configured:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected paired My Tracks settings, got unpaired state",
        )
    return record


def _require_secrets_key_for_pairing() -> None:
    if not secrets_key_configured():
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                "Expected domesti_secrets_key before pairing — run setup-secrets or set "
                "DOMESTI_BOT_SECRETS_KEY"
            ),
        )


def _retention_record_to_schema(
    record: LocationHistoryRetentionRecord,
) -> LocationHistoryRetentionOut:
    return LocationHistoryRetentionOut(
        max_age_hours=record.max_age_hours,
        min_keep_count=record.min_keep_count,
        unlimited=record.unlimited,
    )


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


def _validated_domesti_public_url(url: str) -> str:
    try:
        return normalize_public_base_url(url)
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


def _validated_mytracks_domain(domain: str) -> str:
    try:
        return normalize_mytracks_base_url(domain)
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


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
