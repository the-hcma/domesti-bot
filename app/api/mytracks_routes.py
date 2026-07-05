"""HTTP routes for My Tracks settings and roster/geofence sync."""

from __future__ import annotations

import secrets
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    LocationHistoryRetentionIn,
    LocationHistoryRetentionOut,
    LocationRequestRateLimitsOut,
    MyTracksCredentialsTestIn,
    MyTracksGeofencesSyncOut,
    MyTracksLocationMonitoringIn,
    MyTracksLocationMonitoringOut,
    MyTracksLocationUpdatesIn,
    MyTracksLocationUpdatesOut,
    MyTracksPairIn,
    MyTracksPairStatusOut,
    MyTracksUsersSyncOut,
    MyTracksRelayKeySettingsOut,
    MyTracksSettingsIn,
    MyTracksSettingsOut,
    MyTracksSyncIn,
    SettingsCredentialsTestOut,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    load_mytracks_relay_api_key_from_db,
    mytracks_relay_api_key_stored_in_db,
    save_mytracks_relay_api_key_to_db,
    secrets_key_configured,
)
from app.location_monitoring_policy import approach_request_interval_s
from app.location_request_rate_limits import LocationRequestRateLimits
from app.mytracks_logging import mytracks_log_host, mytracks_logger
from app.mytracks_service import (
    DomestiBotConfigFromMyTracks,
    ExportedUser,
    MyTracksSyncError,
    build_location_update_webhook_urls,
    fetch_geofences_from_my_tracks,
    fetch_mytracks_domesti_config,
    fetch_users_from_my_tracks,
    MyTracksPairResult,
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
    clear_mytracks_pairing,
    delete_mytracks_settings,
    load_approach_monitoring_distance_m,
    load_location_history_retention,
    load_location_request_rate_limits,
    load_mytracks_config,
    load_mytracks_pair_status,
    load_remote_request_location_enabled,
    record_mytracks_geofences_sync,
    record_mytracks_users_sync,
    save_approach_monitoring_distance_m,
    save_location_history_retention,
    save_mytracks_config,
    save_mytracks_pairing,
    set_last_pair_error,
    set_location_request_rate_limits,
    set_location_updates_accepted,
    set_remote_request_location_enabled,
)
from app.presence_store import (
    UserLocationRecord,
    parse_iso_timestamp_to_epoch,
    prune_all_user_location_history,
    replace_user_locations,
)
from app.rules_store import (
    GeofenceRecord,
    UserRecord,
    count_geofences,
    count_users,
    replace_geofences,
    replace_users,
)
from app.settings_credentials_test import (
    CredentialsTestUnavailableError,
    probe_mytracks_credentials,
)

settings_router = APIRouter(prefix="/v1/settings", tags=["settings"])
rules_router = APIRouter(prefix="/v1/rules", tags=["rules"])

_LOGGER = mytracks_logger(__name__)


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


@settings_router.delete("/my-tracks/pair", response_model=MyTracksPairStatusOut | None)
async def delete_mytracks_pair(request: Request) -> MyTracksPairStatusOut | None:
    """Clear pairing metadata and revoke the stored relay API key."""
    cache_path = _require_discovery_cache(request)
    clear_mytracks_pairing(cache_path)
    record = load_mytracks_pair_status(cache_path)
    if record is None:
        return None
    _LOGGER.info("pairing reset")
    return _pair_status_to_schema(record, cache_path=cache_path)


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


@settings_router.get("/my-tracks/relay-key", response_model=MyTracksRelayKeySettingsOut)
async def get_mytracks_relay_key_settings(request: Request) -> MyTracksRelayKeySettingsOut:
    """Return relay API key status (includes stored key when paired)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return MyTracksRelayKeySettingsOut(configured=False, stored_relay_key=None)
    stored = mytracks_relay_api_key_stored_in_db(cache_path)
    if not stored:
        return MyTracksRelayKeySettingsOut(configured=False, stored_relay_key=None)
    try:
        relay_key = load_mytracks_relay_api_key_from_db(cache_path)
    except SecretsDecryptError:
        relay_key = None
    return MyTracksRelayKeySettingsOut(configured=True, stored_relay_key=relay_key)


@settings_router.get(
    "/my-tracks/location-monitoring",
    response_model=MyTracksLocationMonitoringOut,
)
async def get_mytracks_location_monitoring(
    request: Request,
) -> MyTracksLocationMonitoringOut:
    """Return proactive location monitoring settings."""
    cache_path = _require_discovery_cache(request)
    return MyTracksLocationMonitoringOut(
        approach_distance_m=load_approach_monitoring_distance_m(cache_path),
        approach_request_interval_s=approach_request_interval_s(),
    )


@settings_router.patch(
    "/my-tracks/location-monitoring",
    response_model=MyTracksLocationMonitoringOut,
)
async def patch_mytracks_location_monitoring_route(
    body: MyTracksLocationMonitoringIn,
    request: Request,
) -> MyTracksLocationMonitoringOut:
    """Update geofence approach monitoring distance."""
    cache_path = _require_discovery_cache(request)
    _require_mytracks_config(request)
    saved_distance_m = save_approach_monitoring_distance_m(
        cache_path,
        distance_m=body.approach_distance_m,
    )
    return MyTracksLocationMonitoringOut(
        approach_distance_m=saved_distance_m,
        approach_request_interval_s=approach_request_interval_s(),
    )


@settings_router.patch(
    "/my-tracks/location-history-retention",
    response_model=LocationHistoryRetentionOut,
)
async def patch_mytracks_location_history_retention_route(
    body: LocationHistoryRetentionIn,
    request: Request,
) -> LocationHistoryRetentionOut:
    """Update how many locations are kept per user in history."""
    cache_path = _require_discovery_cache(request)
    _require_mytracks_config(request)
    saved = save_location_history_retention(
        cache_path,
        max_age_hours=body.max_age_hours,
        min_keep_count=body.min_keep_count,
        unlimited=body.unlimited,
    )
    retention = load_location_history_retention(cache_path)
    pruned = prune_all_user_location_history(cache_path, retention=retention)
    if pruned:
        _LOGGER.info("location-history retention update pruned %d row(s)", pruned)
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


@settings_router.post("/my-tracks/test", response_model=SettingsCredentialsTestOut)
async def post_mytracks_credentials_test(
    body: MyTracksCredentialsTestIn,
    request: Request,
) -> SettingsCredentialsTestOut:
    """Probe My Tracks admin credentials with a read-only roster fetch."""
    cache_path = discovery_cache_path_from_request(request)
    try:
        result = await probe_mytracks_credentials(
            cache_path=cache_path,
            password=body.password,
            domain=body.domain,
            username=body.username,
        )
    except CredentialsTestUnavailableError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SettingsCredentialsTestOut(
        ok=result.ok,
        detail=result.detail,
        source=result.source,
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
    domesti_public = _resolve_domesti_public_base_url_from_request(request)
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
        "pairing starting for %s as %s (domesti %s)",
        mytracks_log_host(mytracks_base),
        username,
        mytracks_log_host(domesti_public),
    )
    try:
        save_mytracks_relay_api_key_to_db(cache_path, relay_key)
    except SecretsConfigurationError as exc:
        _LOGGER.warning(
            "pairing failed for %s as %s before my-tracks call: %s",
            mytracks_log_host(mytracks_base),
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    try:
        pair_result = pair_with_my_tracks(
            api_key=relay_key,
            base_url=mytracks_base,
            domesti_base_url=domesti_public,
            user_location_update_url=update_url,
            user_location_test_url=test_url,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError as exc:
        set_last_pair_error(cache_path, str(exc))
        _LOGGER.warning(
            "pairing failed for %s as %s: %s",
            mytracks_log_host(mytracks_base),
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
            user_location_update_url=update_url,
            user_location_test_url=test_url,
        ),
    )
    set_remote_request_location_enabled(cache_path, enabled=None)
    set_location_request_rate_limits(cache_path, limits=None)
    try:
        domesti_config = fetch_mytracks_domesti_config(
            base_url=mytracks_base,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError:
        domesti_config = None
    _cache_domesti_admin_config(
        cache_path,
        domesti_config=domesti_config,
        pair_result=pair_result,
    )
    _LOGGER.info(
        "pairing complete for %s as %s (HTTP %d)",
        mytracks_log_host(mytracks_base),
        username,
        pair_result.status_code,
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


@rules_router.get("/users/sync-status", response_model=MyTracksUsersSyncOut)
async def get_mytracks_users_sync_status(
    request: Request,
) -> MyTracksUsersSyncOut:
    """Return last user roster sync metadata from My Tracks settings."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return MyTracksUsersSyncOut(
            last_synced_at=None,
            user_count=0,
        )
    record = load_mytracks_config(cache_path)
    user_count = count_users(cache_path)
    if record is None:
        return MyTracksUsersSyncOut(
            last_synced_at=None,
            user_count=user_count,
        )
    return MyTracksUsersSyncOut(
        last_synced_at=record.last_users_sync_at,
        user_count=user_count,
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
        "geofence sync starting for %s as %s",
        mytracks_log_host(base_url),
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
            "geofence sync failed for %s as %s: %s",
            mytracks_log_host(base_url),
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
        "geofence sync complete for %s: %d geofence(s)",
        mytracks_log_host(base_url),
        count,
    )
    return MyTracksGeofencesSyncOut(
        geofence_count=count,
        last_synced_at=updated.last_geofences_sync_at,
    )


@rules_router.post("/users/sync", response_model=MyTracksUsersSyncOut)
async def post_mytracks_users_sync(
    body: MyTracksSyncIn, request: Request
) -> MyTracksUsersSyncOut:
    """Pull the user roster from My Tracks using admin credentials."""
    cache_path = _require_discovery_cache(request)
    record, username = _resolve_sync_credentials(request, body)
    base_url = normalize_mytracks_base_url(record.domain)
    _LOGGER.info(
        "user sync starting for %s as %s",
        mytracks_log_host(base_url),
        username,
    )
    try:
        exported = fetch_users_from_my_tracks(
            base_url=base_url,
            password=body.password,
            username=username,
        )
    except MyTracksSyncError as exc:
        _LOGGER.warning(
            "user sync failed for %s as %s: %s",
            mytracks_log_host(base_url),
            username,
            exc,
        )
        raise HTTPException(
            status_code=HTTPStatus.BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    count = replace_users(
        cache_path,
        [
            UserRecord(
                user_id=row.user_id,
                first_name=row.first_name,
                last_name=row.last_name,
                display_name=row.display_name,
                tracking_device_label=row.tracking_device_label,
                enabled=row.enabled,
            )
            for row in exported
        ],
    )
    location_count = replace_user_locations(
        cache_path,
        [_user_location_from_export(row) for row in exported if row.latest_location is not None],
        retention=load_location_history_retention(cache_path),
    )
    updated = record_mytracks_users_sync(cache_path, count=count)
    _LOGGER.info(
        "user sync complete for %s: %d user(s), %d location(s)",
        mytracks_log_host(base_url),
        count,
        location_count,
    )
    return MyTracksUsersSyncOut(
        last_synced_at=updated.last_users_sync_at,
        user_count=count,
    )


def _cache_domesti_admin_config(
    cache_path: Path,
    *,
    domesti_config: DomestiBotConfigFromMyTracks | None,
    pair_result: MyTracksPairResult | None = None,
) -> None:
    limits: LocationRequestRateLimits | None = None
    remote_enabled: bool | None = None
    if domesti_config is not None:
        limits = domesti_config.location_request_rate_limits
        remote_enabled = domesti_config.remote_request_location_enabled
    if pair_result is not None:
        if pair_result.location_request_rate_limits is not None:
            limits = pair_result.location_request_rate_limits
        if pair_result.remote_request_location_enabled is not None:
            remote_enabled = pair_result.remote_request_location_enabled
    if remote_enabled is not None:
        set_remote_request_location_enabled(cache_path, enabled=remote_enabled)
    if limits is not None:
        set_location_request_rate_limits(cache_path, limits=limits)


def _cached_rate_limits_out(cache_path: Path) -> LocationRequestRateLimitsOut | None:
    return _rate_limits_out(load_location_request_rate_limits(cache_path))


def _maybe_mytracks_domesti_config_from_admin(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None,
) -> DomestiBotConfigFromMyTracks | None:
    if record.paired_at is None:
        return None
    if password is None or password.strip() == "":
        return None
    base_url = normalize_mytracks_base_url(record.domain)
    try:
        return fetch_mytracks_domesti_config(
            base_url=base_url,
            password=password,
            username=record.username,
        )
    except MyTracksSyncError:
        return None


def _maybe_mytracks_location_updates_enabled(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None,
) -> bool | None:
    config = _maybe_mytracks_domesti_config_from_admin(
        record,
        cache_path=cache_path,
        password=password,
    )
    if config is None:
        return None
    return config.location_updates_enabled


def _maybe_mytracks_remote_request_location_enabled(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None,
) -> bool | None:
    cached = load_remote_request_location_enabled(cache_path)
    if cached is not None and (password is None or password.strip() == ""):
        return cached
    config = _maybe_mytracks_domesti_config_from_admin(
        record,
        cache_path=cache_path,
        password=password,
    )
    if config is None:
        return cached
    remote_enabled = config.remote_request_location_enabled
    if remote_enabled is not None:
        set_remote_request_location_enabled(cache_path, enabled=remote_enabled)
    if config.location_request_rate_limits is not None:
        set_location_request_rate_limits(
            cache_path,
            limits=config.location_request_rate_limits,
        )
    return remote_enabled


def _pair_status_to_schema(
    record: MyTracksPairStatusRecord,
    *,
    cache_path: Path,
    password: str | None = None,
) -> MyTracksPairStatusOut:
    remote_request_enabled = _maybe_mytracks_remote_request_location_enabled(
        record,
        cache_path=cache_path,
        password=password,
    )
    return MyTracksPairStatusOut(
        paired_at=record.paired_at,
        domain=record.domain,
        username=record.username,
        domesti_public_base_url=record.domesti_public_base_url,
        user_location_update_url=record.user_location_update_url,
        user_location_test_url=record.user_location_test_url,
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
        mytracks_location_request_rate_limits=_cached_rate_limits_out(cache_path),
        mytracks_remote_request_location_enabled=remote_request_enabled,
        last_verify_at=record.last_verify_at,
        last_verify_ok=record.last_verify_ok,
        last_pair_error=record.last_pair_error,
    )


def _user_location_from_export(row: ExportedUser) -> UserLocationRecord:
    location = row.latest_location
    if location is None:
        raise ValueError("Expected latest_location, got None")
    return UserLocationRecord(
        user_id=row.user_id,
        lat=location.lat,
        lon=location.lon,
        accuracy_m=location.accuracy_m,
        fix_at=parse_iso_timestamp_to_epoch(location.fix_at),
        reported_at=parse_iso_timestamp_to_epoch(location.reported_at),
        source="my-tracks",
    )


def _resolve_domesti_public_base_url_from_request(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    forwarded_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
    if forwarded_host != "":
        scheme = forwarded_proto if forwarded_proto != "" else "https"
        raw = f"{scheme}://{forwarded_host}"
    else:
        raw = str(request.base_url).rstrip("/")
    try:
        return normalize_public_base_url(raw)
    except MyTracksSyncError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc


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


def _rate_limits_out(
    limits: LocationRequestRateLimits | None,
) -> LocationRequestRateLimitsOut | None:
    if limits is None:
        return None
    return LocationRequestRateLimitsOut(
        device_cooldown_seconds=limits.device_cooldown_seconds,
        user_cooldown_seconds=limits.user_cooldown_seconds,
        user_cooldown_seconds_by_reason=limits.user_cooldown_seconds_by_reason,
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
