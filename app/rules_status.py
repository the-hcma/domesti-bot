"""Assemble ``GET /v1/rules/status`` payloads."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.domesti_bot_cli import DeviceManagersState

from app.api.schemas import (
    GeofenceOut,
    RulesEvaluatorOut,
    RulesStatusOut,
    RuleStatusSummaryOut,
    RuleValidationOut,
    RulesValidationOut,
    UserLocationOut,
    UserStatusOut,
)
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.cron_schedule import fired_on_same_local_calendar_day
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
from app.rule_validation import (
    RosterUserRow,
    RuleValidationContext,
    build_roster_name_hint_lookup,
    build_roster_user_id_lookup,
    validate_rule,
    validate_rules,
)
from app.rules_store import GeofenceRecord, list_geofences, list_users
from app.smtp_store import load_smtp_config, smtp_send_ready


def build_rules_validation(
    *,
    cache_path: Path | None,
    device_state: DeviceManagersState | None = None,
) -> RulesValidationOut:
    """Cross-check file-backed rules against persisted roster, geofences, and devices."""
    geofences = _load_geofences(cache_path)
    users = _load_users_status(cache_path)
    validation_ctx = _build_validation_context(
        cache_path=cache_path,
        device_state=device_state,
        geofences=geofences,
        users=users,
    )
    issues_by_rule = validate_rules(list_automation_rules(), validation_ctx)
    return RulesValidationOut(
        rules=[
            RuleValidationOut(id=rule_id, issues=issues)
            for rule_id, issues in issues_by_rule.items()
            if issues
        ],
    )


def build_rules_status(
    *,
    cache_path: Path | None,
    device_state: DeviceManagersState | None = None,
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
    roster_user_id_lookup = build_roster_user_id_lookup(
        [row.user_id for row in users],
    )
    inside_since = (
        evaluator.geofence_inside_since_snapshot()
        if evaluator is not None
        else {}
    )
    eval_ctx = RuleEvaluationContext(
        device_state=device_state,
        geofence_inside_since=inside_since,
        geofences=tuple(geofences),
        now=effective_now,
        roster_user_id_lookup=roster_user_id_lookup,
        sun=sun,
        timezone=tz,
        user_display_names=user_display_names,
        user_locations=user_locations,
    )
    validation_ctx = _build_validation_context(
        cache_path=cache_path,
        device_state=device_state,
        geofences=geofences,
        users=users,
    )

    rules = list_automation_rules()
    rule_rows: list[RuleStatusSummaryOut] = []
    for rule in rules:
        evaluation = evaluate_rule(rule, eval_ctx)
        fire_state = _fire_state_for_rule(evaluator, rule.id)
        next_evaluate_at: str | None = None
        scheduled_detail: str | None = None
        if rule.trigger == "scheduled" and rule.enabled and evaluator is not None:
            scheduled_at = evaluator.next_evaluate_at_for_rule(rule.id)
            if scheduled_at is not None:
                next_evaluate_at = _epoch_to_iso_z(scheduled_at)
        if (
            rule.trigger == "scheduled"
            and rule.fire_once_per_local_day
            and fire_state.last_fired_at is not None
            and fired_on_same_local_calendar_day(
                fire_state.last_fired_at,
                effective_now.timestamp(),
                tz,
            )
        ):
            scheduled_detail = (
                "Already fired today (next eligible after local midnight)"
            )
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
                next_evaluate_at=next_evaluate_at,
                reference_issues=validate_rule(rule, validation_ctx),
                scheduled_detail=scheduled_detail,
                trigger=rule.trigger,
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
    )


def _build_validation_context(
    *,
    cache_path: Path | None,
    device_state: DeviceManagersState | None,
    geofences: list[GeofenceOut],
    users: list[UserStatusOut],
) -> RuleValidationContext:
    roster_users = [
        RosterUserRow(
            display_name=row.display_name,
            first_name=row.first_name,
            user_id=row.user_id,
        )
        for row in users
    ]
    smtp_configured = False
    if cache_path is not None:
        smtp_record = load_smtp_config(cache_path)
        smtp_configured = smtp_send_ready(smtp_record)
    return RuleValidationContext(
        device_state=device_state,
        geofence_ids=frozenset(row.geofence_id for row in geofences),
        roster_name_hint_lookup=build_roster_name_hint_lookup(roster_users),
        roster_user_id_lookup=build_roster_user_id_lookup(
            [row.user_id for row in users],
        ),
        smtp_configured=smtp_configured,
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
