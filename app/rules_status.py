"""Assemble ``GET /v1/rules/status`` payloads."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from app.api.schemas import (
    GeofenceOut,
    ParticipantFixOut,
    ParticipantStatusOut,
    RulesEvaluatorOut,
    RulesStatusOut,
    RuleStatusSummaryOut,
)
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.presence_store import (
    ParticipantFixRecord,
    geofence_ids_containing_fix,
    list_participant_fixes,
)
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rules_store import GeofenceRecord, list_geofences, list_participants

def build_rules_status(
    *,
    cache_path: Path | None,
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
    participants = _load_participants_status(cache_path)
    participant_fixes = _participant_fixes_from_status(participants)
    participant_display_names = {
        row.participant_id: row.display_name for row in participants
    }
    ctx = RuleEvaluationContext(
        geofences=tuple(geofences),
        now=effective_now,
        participant_display_names=participant_display_names,
        participant_fixes=participant_fixes,
        sun=sun,
        timezone=tz,
    )

    rules = list_automation_rules()
    rule_rows: list[RuleStatusSummaryOut] = []
    for rule in rules:
        evaluation = evaluate_rule(rule, ctx)
        rule_rows.append(
            RuleStatusSummaryOut(
                condition_currently_true=evaluation.all_met,
                conditions=evaluation.conditions,
                enabled=rule.enabled,
                id=rule.id,
                label=rule.label,
                last_error=None,
                last_fired_at=None,
            )
        )

    now_utc = datetime.now(UTC)
    return RulesStatusOut(
        evaluator=RulesEvaluatorOut(
            last_run_at=_to_iso_z(now_utc),
            next_sun_check_at=_to_iso_z(now_utc + timedelta(minutes=1)),
        ),
        geofences=geofences,
        participants=participants,
        rules=rule_rows,
        sun=sun,
        using_mock=False,
    )


def _fix_received_at_iso(fix: ParticipantFixRecord) -> str:
    return datetime.fromtimestamp(fix.received_at, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


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


def _load_participants_status(
    cache_path: Path | None,
) -> list[ParticipantStatusOut]:
    if cache_path is None:
        return []
    participants = list_participants(cache_path)
    fixes = list_participant_fixes(cache_path)
    geofences = list_geofences(cache_path)
    now = time.time()
    rows: list[ParticipantStatusOut] = []
    for participant in participants:
        fix = fixes.get(participant.participant_id)
        last_fix: ParticipantFixOut | None = None
        age_seconds: int | None = None
        inside_geofence_ids: list[str] = []
        if fix is not None:
            received_at = _fix_received_at_iso(fix)
            last_fix = ParticipantFixOut(
                accuracy_m=fix.accuracy_m,
                lat=fix.lat,
                lon=fix.lon,
                received_at=received_at,
                source=fix.source,
            )
            age_seconds = max(0, int(now - fix.received_at))
            inside_geofence_ids = geofence_ids_containing_fix(fix, geofences)
        rows.append(
            ParticipantStatusOut(
                age_seconds=age_seconds,
                display_name=participant.display_name,
                enabled=participant.enabled,
                inside_geofence_ids=inside_geofence_ids,
                last_fix=last_fix,
                participant_id=participant.participant_id,
                tracking_device_label=participant.tracking_device_label,
            )
        )
    return rows


def _participant_fixes_from_status(
    participants: list[ParticipantStatusOut],
) -> dict[str, ParticipantFixOut]:
    fixes: dict[str, ParticipantFixOut] = {}
    for participant in participants:
        if participant.last_fix is not None:
            fixes[participant.participant_id] = participant.last_fix
    return fixes


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")
