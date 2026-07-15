"""HTTP routes for persisted Automations users and geofences."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    GeofenceOut,
    ObservedWifiNetworkOut,
    RuleOut,
    RulesStatusOut,
    RulesValidationOut,
    SettingsLocationIn,
    SettingsLocationOut,
    UserHomeWifiIn,
    UserLocationOut,
    UserOut,
    UserStatusOut,
    VacationModeSettingsOut,
    VacationModeSettingsStatusOut,
    VacationModeTestEmailIn,
    VacationModeTestEmailOut,
)
from app.automation_rules_loader import (
    AutomationRulesLoadError,
    list_automation_rules,
    load_settings_location,
    load_vacation_mode_settings,
    save_settings_location,
    save_vacation_mode_settings,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.server_runtime import runtime
from app.location_report import location_epoch_to_iso_z
from app.presence_store import (
    UserLocationRecord,
    list_observed_wifi_networks_for_user,
    list_user_locations,
)
from app.wifi_home_presence import (
    effective_geofence_ids_containing_location,
    geofence_presence_accuracy_limit_m,
)
from app.rules_status import build_rules_status, build_rules_validation
from app.rules_store import (
    GeofenceRecord,
    UserRecord,
    delete_geofence,
    list_geofences,
    list_users,
    save_geofence,
    set_user_home_wifi,
    user_exists,
)
from app.device_enums import VacationEmailSource
from app.vacation_mode import send_vacation_mode_transition_email
from app.vacation_mode_store import load_vacation_mode_state

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


@router.get(
    "/users/{user_id}/observed-wifi",
    response_model=list[ObservedWifiNetworkOut],
)
async def get_user_observed_wifi(user_id: str, request: Request) -> list[ObservedWifiNetworkOut]:
    """Return distinct WiFi networks seen in ``user_id`` location history."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    trimmed_user_id = user_id.strip()
    if not user_exists(cache_path, trimmed_user_id):
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown user_id {trimmed_user_id!r}",
        )
    return [
        ObservedWifiNetworkOut(
            wifi_ssid=network.wifi_ssid,
            wifi_bssid=network.wifi_bssid,
            last_seen_at=_epoch_to_iso_z(network.last_seen_at),
        )
        for network in list_observed_wifi_networks_for_user(cache_path, trimmed_user_id)
    ]


@router.put("/users/{user_id}/home-wifi", response_model=UserOut)
async def put_user_home_wifi(
    user_id: str,
    body: UserHomeWifiIn,
    request: Request,
) -> UserOut:
    """Set or clear the home WiFi network for ``user_id``."""
    cache_path = _require_discovery_cache(request)
    trimmed_user_id = user_id.strip()
    try:
        saved = set_user_home_wifi(
            cache_path,
            trimmed_user_id,
            wifi_ssid=body.wifi_ssid,
            wifi_bssid=body.wifi_bssid,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown user_id {trimmed_user_id!r}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return _user_to_schema(saved)


@router.get("/users/status", response_model=list[UserStatusOut])
async def get_users_status(request: Request) -> list[UserStatusOut]:
    """Return user roster rows enriched with stored locations."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return []
    return _users_status(cache_path)


@router.get("/settings/location", response_model=SettingsLocationOut)
async def get_rules_settings_location() -> SettingsLocationOut:
    """Return configured home coordinates (distance / astronomy origin).

    ``home_configured`` is false when lat/lon are the ``0.0``/``0.0`` sentinel.
    """
    return _load_settings_location_or_http_error()


@router.put("/settings/location", response_model=SettingsLocationOut)
async def put_rules_settings_location(body: SettingsLocationIn) -> SettingsLocationOut:
    """Update home coordinates in the operator ``automation-rules.json`` bundle."""
    try:
        return save_settings_location(body)
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc


@router.get(
    "/settings/vacation-mode",
    response_model=VacationModeSettingsStatusOut,
)
async def get_rules_settings_vacation_mode(
    request: Request,
) -> VacationModeSettingsStatusOut:
    """Return vacation-mode config and the persisted armed latch bit."""
    return _vacation_mode_status(request)


@router.put(
    "/settings/vacation-mode",
    response_model=VacationModeSettingsStatusOut,
)
async def put_rules_settings_vacation_mode(
    body: VacationModeSettingsOut,
    request: Request,
) -> VacationModeSettingsStatusOut:
    """Update vacation-mode config in the operator ``automation-rules.json``."""
    try:
        save_vacation_mode_settings(body)
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc
    return _vacation_mode_status(request)


@router.post(
    "/settings/vacation-mode/test",
    response_model=VacationModeTestEmailOut,
)
async def post_rules_settings_vacation_mode_test(
    body: VacationModeTestEmailIn,
    request: Request,
) -> VacationModeTestEmailOut:
    """Send a sample vacation on/off email without flipping the latch."""
    cache_path = _require_discovery_cache(request)
    try:
        settings = load_vacation_mode_settings()
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc
    try:
        sent = send_vacation_mode_transition_email(
            cache_path,
            armed=body.armed,
            settings=settings,
            source=VacationEmailSource.SETTINGS_TEST,
        )
    except Exception as exc:
        return VacationModeTestEmailOut(
            ok=False,
            message=str(exc),
        )
    if not sent:
        return VacationModeTestEmailOut(
            ok=False,
            message=(
                "Vacation transition email was skipped — configure notification_emails "
                "and SMTP under Automations → Mail"
            ),
        )
    kind = "on" if body.armed else "off"
    return VacationModeTestEmailOut(
        ok=True,
        message=f"Vacation mode {kind} test email sent",
    )


@router.get("/status", response_model=RulesStatusOut)
async def get_rules_status(request: Request) -> RulesStatusOut:
    """Return evaluated rule conditions for the Automations Status tab."""
    cache_path = discovery_cache_path_from_request(request)
    evaluator = runtime.rule_evaluator
    try:
        return build_rules_status(
            cache_path=cache_path,
            device_state=runtime.device_state,
            evaluator=evaluator,
        )
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc


@router.get("/validation", response_model=RulesValidationOut)
async def get_rules_validation(request: Request) -> RulesValidationOut:
    """Return broken user/geofence references in file-backed automation rules."""
    cache_path = discovery_cache_path_from_request(request)
    try:
        return build_rules_validation(
            cache_path=cache_path,
            device_state=runtime.device_state,
        )
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


def _epoch_to_iso_z(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _user_to_schema(record: UserRecord) -> UserOut:
    return UserOut(
        display_name=record.display_name,
        enabled=record.enabled,
        first_name=record.first_name,
        home_wifi_bssid=record.home_wifi_bssid,
        home_wifi_ssid=record.home_wifi_ssid,
        last_name=record.last_name,
        tracking_device_label=record.tracking_device_label,
        user_id=record.user_id,
    )


def _users_status(cache_path: Path) -> list[UserStatusOut]:
    users = list_users(cache_path)
    locations = list_user_locations(cache_path)
    geofences = list_geofences(cache_path)
    settings = load_settings_location()
    try:
        rules = list_automation_rules()
    except AutomationRulesLoadError:
        rules = []
    min_accuracy_m = geofence_presence_accuracy_limit_m(rules)
    now = time.time()
    rows: list[UserStatusOut] = []
    for user in users:
        location = locations.get(user.user_id)
        last_location: UserLocationOut | None = None
        age_seconds: int | None = None
        inside_geofence_ids: list[str] = []
        if location is not None:
            fix_at = location_epoch_to_iso_z(location.fix_at)
            reported_at = location_epoch_to_iso_z(location.reported_at)
            last_location = UserLocationOut(
                accuracy_m=location.accuracy_m,
                battery_level=location.battery_level,
                connection_type=location.connection_type,
                fix_at=fix_at,
                fix_source=location.fix_source,
                lat=location.lat,
                lon=location.lon,
                reported_at=reported_at,
                source=location.source,
                trigger=location.trigger,
                wifi_bssid=location.wifi_bssid,
                wifi_ssid=location.wifi_ssid,
            )
            age_seconds = max(0, int(now - location.reported_at))
            inside_geofence_ids = effective_geofence_ids_containing_location(
                location,
                geofences,
                settings=settings,
                min_accuracy_m=min_accuracy_m,
                home_wifi_bssid=user.home_wifi_bssid,
            )
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


def _vacation_mode_status(request: Request) -> VacationModeSettingsStatusOut:
    try:
        settings = load_vacation_mode_settings()
    except AutomationRulesLoadError as exc:
        raise _automation_rules_http_error(exc) from exc
    cache_path = discovery_cache_path_from_request(request)
    armed = False
    if cache_path is not None:
        armed = load_vacation_mode_state(cache_path).armed
    return VacationModeSettingsStatusOut(
        **settings.model_dump(),
        armed=armed,
    )
