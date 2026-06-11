"""Assemble ``GET /v1/rules/status`` payloads."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.api.schemas import (
    GeofenceOut,
    RulesEvaluatorOut,
    RulesStatusOut,
    RuleStatusSummaryOut,
    UserLocationOut,
    UserStatusOut,
)
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_locations,
)
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rule_evaluator import RuleEvaluator, RuleEvaluatorFireState
from app.rules_store import GeofenceRecord, list_geofences, list_users


def build_rules_status(
    *,
    cache_path: Path | None,
    evaluator: RuleEvaluator | None = None,
    now: datetime | None = None,
) -> RulesStatusOut:
    """Evaluate file-backed rules against persisted presence rows."""
    settings = load_settings_location()
    tz = ZoneInfo(settings.timezone)
    if now is None:
        effective_now = datetime.now(tz)
    elif now.tzinfo is None:
        effective_now = now.replace(tzinfo=tz)
    else:
        effective_now = now.astimezone(tz)

    sun = compute_rules_sun_out(settings, now=effective_now)
    geofences = _load_geofences(cache_path)
    users = _load_users_status(cache_path)
    user_locations = _user_locations_from_status(users)
    user_display_names = {row.user_id: row.display_name for row in users}
    ctx = RuleEvaluationContext(
        geofences=tuple(geofences),
        now=effective_now,
        sun=sun,
        timezone=tz,
        user_display_names=user_display_names,
        user_locations=user_locations,
    )

    rules = list_automation_rules()
    rule_rows: list[RuleStatusSummaryOut] = []
    for rule in rules:
        evaluation = evaluate_rule(rule, ctx)
        fire_state = _fire_state_for_rule(evaluator, rule.id)
        rule_rows.append(
            RuleStatusSummaryOut(
                condition_currently_true=evaluation.all_met,
                conditions=evaluation.conditions,
                enabled=rule.enabled,
                id=rule.id,
                label=rule.label,
                last_error=fire_state.last_error,
                last_fired_at=(
                    _epoch_to_iso_z(fire_state.last_fired_at)
                    if fire_state.last_fired_at is not None
                    else None
                ),
            )
        )

    now_utc = datetime.now(UTC)
    evaluator_last_run = (
        _epoch_to_iso_z(evaluator.last_run_at)
        if evaluator is not None and evaluator.last_run_at is not None
        else _to_iso_z(now_utc)
    )
    evaluator_next_sun = (
        _epoch_to_iso_z(evaluator.next_sun_check_at)
        if evaluator is not None and evaluator.next_sun_check_at is not None
        else _to_iso_z(now_utc + timedelta(minutes=1))
    )
    return RulesStatusOut(
        evaluator=RulesEvaluatorOut(
            last_run_at=evaluator_last_run,
            next_sun_check_at=evaluator_next_sun,
        ),
        geofences=geofences,
        rules=rule_rows,
        sun=sun,
        users=users,
        using_mock=False,
    )


def _epoch_to_iso_z(epoch: float) -> str:
    return _to_iso_z(datetime.fromtimestamp(epoch, tz=UTC))


def _fire_state_for_rule(
    evaluator: RuleEvaluator | None,
    rule_id: str,
) -> RuleEvaluatorFireState:
    if evaluator is None:
        return RuleEvaluatorFireState()
    return evaluator.fire_state_for_rule(rule_id)


def _geofence_to_schema(record: GeofenceRecord) -> GeofenceOut:
    return GeofenceOut(
        center_lat=record.center_lat,
        center_lon=record.center_lon,
        enabled=record.enabled,
        geofence_id=record.geofence_id,
        label=record.label,
        owntracks_rid=record.owntracks_rid,
        radius_m=record.radius_m,
    )


def _load_geofences(cache_path: Path | None) -> list[GeofenceOut]:
    if cache_path is None:
        return []
    return [_geofence_to_schema(row) for row in list_geofences(cache_path)]


def _load_users_status(cache_path: Path | None) -> list[UserStatusOut]:
    if cache_path is None:
        return []
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


def _location_received_at_iso(location: UserLocationRecord) -> str:
    return datetime.fromtimestamp(location.received_at, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _user_locations_from_status(
    users: list[UserStatusOut],
) -> dict[str, UserLocationOut]:
    locations: dict[str, UserLocationOut] = {}
    for user in users:
        if user.last_location is not None:
            locations[user.user_id] = user.last_location
    return locations
