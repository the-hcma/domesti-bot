"""HTTP routes for persisted Automations users and geofences."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    GeofenceOut,
    RuleOut,
    RulesStatusOut,
    SettingsLocationOut,
    UserLocationOut,
    UserOut,
    UserStatusOut,
)
from app.automation_rules_loader import (
    AutomationRulesLoadError,
    list_automation_rules,
    load_settings_location,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_locations,
)
from app.rules_status import build_rules_status
from app.rules_store import (
    GeofenceRecord,
    UserRecord,
    delete_geofence,
    list_geofences,
    list_users,
    save_geofence,
)

router = APIRouter(prefix="/v1/rules", tags=["rules"])


@router.delete("/geofences/{geofence_id}", status_code=HTTPStatus.NO_CONTENT)
async def delete_geofence_route(geofence_id: str, request: Request) -> None:
    """Remove one geofence row."""
    cache_path = _require_discovery_cache(request)
    delete_geofence(cache_path, geofence_id)


@router.get("", response_model=list[RuleOut])
async def get_rules() -> list[RuleOut]:
    """Return automation rules from ``automation-rules.json`` (or the example template)."""
    return _load_rules_or_http_error()


@router.get("/geofences", response_model=list[GeofenceOut])
async def get_geofences(request: Request) -> list[GeofenceOut]:
    """Return persisted geofence definitions."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return [_geofence_to_schema(row) for row in list_geofences(cache_path)]


@router.get("/users", response_model=list[UserOut])
async def get_users(request: Request) -> list[UserOut]:
    """Return persisted user roster rows."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return [_user_to_schema(row) for row in list_users(cache_path)]


@router.get("/users/status", response_model=list[UserStatusOut])
async def get_users_status(request: Request) -> list[UserStatusOut]:
    """Return user roster rows enriched with stored locations."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return _users_status(cache_path)


@router.get("/settings/location", response_model=SettingsLocationOut)
async def get_rules_settings_location() -> SettingsLocationOut:
    """Return home coordinates from the automation rule bundle."""
    return _load_settings_location_or_http_error()


@router.get("/status", response_model=RulesStatusOut)
async def get_rules_status(request: Request) -> RulesStatusOut:
    """Return evaluated rule conditions for the Automations Status tab."""
    cache_path = discovery_cache_path_from_request(request)
    try:
        return build_rules_status(cache_path=cache_path)
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc


@router.get("/{rule_id}", response_model=RuleOut)
async def get_rule(rule_id: str) -> RuleOut:
    """Return one automation rule from the file-backed bundle."""
    for rule in _load_rules_or_http_error():
        if rule.id == rule_id:
            return rule
    raise HTTPException(
        status_code=HTTPStatus.NOT_FOUND,
        detail=f"Expected rule id, got unknown {rule_id!r}",
    )


@router.put("/geofences/{geofence_id}", response_model=GeofenceOut)
async def put_geofence(
    geofence_id: str,
    body: GeofenceOut,
    request: Request,
) -> GeofenceOut:
    """Create or update one geofence row."""
    cache_path = _require_discovery_cache(request)
    if body.geofence_id != geofence_id:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                f"Expected geofence_id in path to match body, got {geofence_id!r} "
                f"and {body.geofence_id!r}"
            ),
        )
    saved = save_geofence(
        cache_path,
        GeofenceRecord(
            geofence_id=body.geofence_id,
            label=body.label.strip(),
            center_lat=body.center_lat,
            center_lon=body.center_lon,
            radius_m=body.radius_m,
            enabled=body.enabled,
            owntracks_rid=body.owntracks_rid,
        ),
    )
    return _geofence_to_schema(saved)


def _automation_rules_http_error(exc: AutomationRulesLoadError) -> HTTPException:
    return HTTPException(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
        detail=str(exc),
    )


def _geofence_to_schema(record: GeofenceRecord) -> GeofenceOut:
    return GeofenceOut(
        geofence_id=record.geofence_id,
        label=record.label,
        center_lat=record.center_lat,
        center_lon=record.center_lon,
        radius_m=record.radius_m,
        enabled=record.enabled,
        owntracks_rid=record.owntracks_rid,
    )


def _location_received_at_iso(location: UserLocationRecord) -> str:
    return datetime.fromtimestamp(location.received_at, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _user_to_schema(record: UserRecord) -> UserOut:
    return UserOut(
        display_name=record.display_name,
        enabled=record.enabled,
        first_name=record.first_name,
        last_name=record.last_name,
        tracking_device_label=record.tracking_device_label,
        user_id=record.user_id,
    )


def _users_status(cache_path: Path) -> list[UserStatusOut]:
    users = list_users(cache_path)
    locations = list_user_locations(cache_path)
    geofences = list_geofences(cache_path)
    now = time.time()
    rows: list[UserStatusOut] = []
    for user in users:
        location = locations.get(user.user_id)
        last_location: UserLocationOut | None = None
        age_seconds: int | None = None
        inside_geofence_ids: list[str] = []
        if location is not None:
            received_at = _location_received_at_iso(location)
            last_location = UserLocationOut(
                accuracy_m=location.accuracy_m,
                lat=location.lat,
                lon=location.lon,
                received_at=received_at,
                source=location.source,
            )
            age_seconds = max(0, int(now - location.received_at))
            inside_geofence_ids = geofence_ids_containing_location(location, geofences)
        rows.append(
            UserStatusOut(
                age_seconds=age_seconds,
                display_name=user.display_name,
                enabled=user.enabled,
                first_name=user.first_name,
                inside_geofence_ids=inside_geofence_ids,
                last_location=last_location,
                last_name=user.last_name,
                tracking_device_label=user.tracking_device_label,
                user_id=user.user_id,
            )
        )
    return rows


def _load_rules_or_http_error() -> list[RuleOut]:
    try:
        return list_automation_rules()
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc


def _load_settings_location_or_http_error() -> SettingsLocationOut:
    try:
        return load_settings_location()
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc


def _require_discovery_cache(request: Request) -> Path:
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Automations data: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    return cache_path
