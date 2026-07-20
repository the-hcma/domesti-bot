"""Server-side automation rule condition evaluation for the Status tab."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from astral import LocationInfo
from astral.sun import sun

from app.api.schemas import (
    AfterLocalTimeCondition,
    AfterSunsetCondition,
    AllConditionsCondition,
    AnyConditionsCondition,
    BeforeLocalTimeCondition,
    BeforeSunriseCondition,
    DaylightCondition,
    DaysOfWeekCondition,
    DevicesAllInStateCondition,
    DevicesAnyInStateCondition,
    DevicesAnyInStateForSCondition,
    GeofenceOut,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionStatusOut,
    RuleOut,
    RulesSunOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersMinDistanceFromHomeMCondition,
    UsersOutsideGeofenceCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.automation_rules_loader import load_settings_location
from app.device_display import format_device_display
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    RuleEvaluationCause,
    RuleTrigger,
)
from app.home_location import try_resolve_home_location
from app.rule_actions import (
    cached_ep1_is_occupied,
    cached_kasa_is_on,
    cached_sonos_is_playing,
    cached_tailwind_is_open,
    cached_vizio_is_on,
    lookup_preferred_label,
)
from app.rule_validation import resolve_device_ref_to_backend_id, resolve_roster_user_id
from app.wifi_home_presence import wifi_home_presence_applies

if TYPE_CHECKING:
    from app.domesti_bot_cli import DeviceManagersState

_LOGGER = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_DAY_NAMES = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")
LOCATION_HISTORY_WALKBACK_MAX_S = 600.0


@dataclass(frozen=True)
class RuleEvaluationContext:
    """Inputs required to evaluate one rule's conditions."""

    geofences: tuple[GeofenceOut, ...]
    now: datetime
    roster_user_id_lookup: dict[str, str]
    sun: RulesSunOut
    timezone: ZoneInfo
    user_display_names: dict[str, str]
    user_locations: dict[str, UserLocationOut]
    device_bool_since: dict[tuple[DeviceFamilyId, str], float] = field(
        default_factory=dict,
    )
    device_bool_value: dict[tuple[DeviceFamilyId, str], bool] = field(
        default_factory=dict,
    )
    device_state: DeviceManagersState | None = None
    geofence_inside_since: dict[tuple[str, str], float] = field(default_factory=dict)
    geofence_outside_since: dict[tuple[str, str], float] = field(default_factory=dict)
    geofence_presence_episode: dict[tuple[str, str], int] = field(default_factory=dict)
    scheduled_inside_dwell_consumed_episode: dict[tuple[str, str, str], int] = field(
        default_factory=dict,
    )
    scheduled_outside_dwell_consumed_episode: dict[tuple[str, str, str], int] = field(
        default_factory=dict,
    )
    user_home_wifi_bssid: dict[str, str | None] = field(default_factory=dict)
    user_location_history: dict[str, tuple[UserLocationOut, ...]] = field(
        default_factory=dict,
    )
    walkback_max_s: float = LOCATION_HISTORY_WALKBACK_MAX_S
    # Why this pass is running; edge-triggered passes evaluate geofence rows as
    # enter/leave edges, while scheduled passes treat them as steady state.
    triggered_by: RuleEvaluationCause = RuleEvaluationCause.EDGE

    def resolve_user_id(self, reference: str) -> str | None:
        return resolve_roster_user_id(reference, self.roster_user_id_lookup)


@dataclass(frozen=True)
class RuleEvaluationResult:
    """Per-rule condition evaluation for the Status tab."""

    all_met: bool
    conditions: list[RuleConditionStatusOut]


def compute_rules_sun_out(
    settings: SettingsLocationOut,
    *,
    now: datetime | None = None,
) -> RulesSunOut:
    """Return today's sunrise/sunset at the configured home location."""
    tz = ZoneInfo(settings.timezone)
    effective_now = _coerce_now(now, tz)
    location = LocationInfo(
        "",
        "",
        settings.timezone,
        settings.lat,
        settings.lon,
    )
    solar = sun(location.observer, date=effective_now.date(), tzinfo=tz)
    sunrise_at = solar["sunrise"]
    sunset_at = solar["sunset"]
    is_dark = effective_now < sunrise_at or effective_now >= sunset_at
    return RulesSunOut(
        is_dark=is_dark,
        sunrise_at=_to_iso_z(sunrise_at),
        sunset_at=_to_iso_z(sunset_at),
    )


def consume_scheduled_dwell_episodes_for_fire(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
    *,
    consumed_inside: dict[tuple[str, str, str], int],
    consumed_outside: dict[tuple[str, str, str], int],
) -> None:
    """Record the current geofence presence episode for each dwell condition user."""
    for condition in iter_dwell_for_s_conditions(rule.conditions.all):
        if isinstance(condition, UsersOutsideGeofenceForSCondition):
            target = consumed_outside
        else:
            target = consumed_inside
        for rule_user_id in condition.user_ids:
            roster_user_id = ctx.resolve_user_id(rule_user_id)
            if roster_user_id is None:
                continue
            episode = ctx.geofence_presence_episode.get(
                (roster_user_id, condition.geofence_id),
                0,
            )
            target[(rule.id, roster_user_id, condition.geofence_id)] = episode


def evaluate_rule(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleEvaluationResult:
    """Evaluate top-level ``conditions.all`` rows for one rule."""
    conditions = [_evaluate_condition(condition, rule, ctx) for condition in rule.conditions.all]
    if RuleTrigger.EDGE_TRUE in rule.triggers and ctx.triggered_by == RuleEvaluationCause.EDGE:
        steady_rows = [
            row
            for row, condition in zip(conditions, rule.conditions.all, strict=True)
            if _counts_for_steady_armed_state(condition)
        ]
        if steady_rows:
            all_met = rule.enabled and all(row.met for row in steady_rows)
        else:
            all_met = rule.enabled
    else:
        all_met = rule.enabled and all(row.met for row in conditions)
    return RuleEvaluationResult(
        all_met=all_met,
        conditions=conditions,
    )


def evaluate_rule_conditions_met(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> bool:
    """Return whether every top-level condition is currently met."""
    return evaluate_rule(rule, ctx).all_met


def users_any_inside_home_geofence(
    *,
    ctx: RuleEvaluationContext,
    min_location_accuracy_m: int,
    user_ids: Sequence[str],
    home_ids: frozenset[str],
) -> bool:
    """Return whether any listed user currently counts as inside a home geofence.

    ``home_ids`` must come from :func:`app.wifi_home_presence.home_geofence_ids`
    (or an empty set when unresolved). Uses the same accuracy / WiFi-home rules
    as ``users_inside_geofence``. Fail closed when ``home_ids`` is empty.
    """
    if not home_ids:
        return False
    now_epoch = ctx.now.timestamp()
    for geofence_id in home_ids:
        geofence = next(
            (row for row in ctx.geofences if row.geofence_id == geofence_id),
            None,
        )
        if geofence is None or not geofence.enabled:
            continue
        for rule_user_id in user_ids:
            roster_user_id = ctx.resolve_user_id(rule_user_id)
            if roster_user_id is None:
                continue
            location = ctx.user_locations.get(roster_user_id)
            if location is None:
                continue
            effective = _resolved_location_for_geofence_rule(
                location,
                ctx.user_location_history.get(roster_user_id, ()),
                geofence_id=geofence_id,
                min_accuracy_m=min_location_accuracy_m,
                now_epoch=now_epoch,
                ctx=ctx,
                roster_user_id=roster_user_id,
            )
            if effective is None:
                continue
            counts_inside, _used_wifi = _user_counts_inside_geofence_for_rule(
                effective,
                geofence,
                geofence_id,
                min_location_accuracy_m,
                ctx,
                roster_user_id,
            )
            if counts_inside:
                return True
    return False


def users_min_distance_from_home_met(
    *,
    ctx: RuleEvaluationContext,
    min_distance_m: float,
    min_location_accuracy_m: int,
    user_ids: Sequence[str],
) -> bool:
    """Return whether every listed user is at least ``min_distance_m`` from home.

    Fail closed when home is unconfigured, a roster id is unknown, or a usable
    location is missing / too inaccurate / stale. Shared by the rule condition
    and the vacation-mode latch.
    """
    home = try_resolve_home_location(load_settings_location())
    if home is None:
        _LOGGER.warning(
            "[rules] users_min_distance_from_home_met: home location is not "
            "configured (settings_location lat/lon) — treating as unmet",
        )
        return False
    now_epoch = ctx.now.timestamp()
    for rule_user_id in user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            return False
        latest = ctx.user_locations.get(roster_user_id)
        if latest is None:
            return False
        effective = _effective_location_for_rule(
            latest,
            ctx.user_location_history.get(roster_user_id, ()),
            min_accuracy_m=min_location_accuracy_m,
            now_epoch=now_epoch,
            walkback_max_s=ctx.walkback_max_s,
        )
        if effective is None:
            return False
        distance_m = _haversine_m(
            effective.lat,
            effective.lon,
            home.lat,
            home.lon,
        )
        if distance_m < min_distance_m:
            return False
    return True


def _coerce_now(now: datetime | None, tz: ZoneInfo) -> datetime:
    if now is None:
        return datetime.now(tz)
    if now.tzinfo is None:
        return now.replace(tzinfo=tz)
    return now.astimezone(tz)


def _conditions_are_presence_only(
    conditions: list[RuleConditionOut],
) -> bool:
    if not conditions:
        return False
    return all(
        isinstance(
            condition,
            (
                UsersInsideGeofenceCondition,
                UsersInsideGeofenceForSCondition,
                UsersOutsideGeofenceCondition,
            ),
        )
        for condition in conditions
    )


def _cached_device_is_occupied(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
) -> bool | None:
    if ctx.device_state is None:
        return None
    if ref.family_id != DeviceFamilyId.EP1:
        return None
    return cached_ep1_is_occupied(ctx.device_state, ref.device_id)


def _cached_device_is_on(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
) -> bool | None:
    if ctx.device_state is None:
        return None
    state = ctx.device_state
    match ref.family_id:
        case DeviceFamilyId.KASA:
            return cached_kasa_is_on(state, ref.device_id)
        case DeviceFamilyId.SONOS:
            return cached_sonos_is_playing(state, ref.device_id)
        case DeviceFamilyId.VIZIO:
            return cached_vizio_is_on(state, ref.device_id)
        case _:
            return None


def _counts_for_steady_armed_state(condition: RuleConditionOut) -> bool:
    if isinstance(
        condition,
        (
            UsersInsideGeofenceCondition,
            UsersInsideGeofenceForSCondition,
            UsersOutsideGeofenceCondition,
            UsersOutsideGeofenceForSCondition,
        ),
    ):
        return False
    if isinstance(condition, AllConditionsCondition):
        return any(_counts_for_steady_armed_state(child) for child in condition.conditions)
    if isinstance(condition, AnyConditionsCondition):
        return any(_counts_for_steady_armed_state(child) for child in condition.conditions)
    return True


def _accurate_inside_from_location(
    location: UserLocationOut | None,
    geofence: GeofenceOut,
    geofence_id: str,
    min_accuracy_m: int,
    ctx: RuleEvaluationContext,
    roster_user_id: str,
) -> bool | None:
    """Return inside/outside from an accurate location reading, or None when unusable."""
    if location is None:
        return None
    counts_inside, used_wifi = _user_counts_inside_geofence_for_rule(
        location,
        geofence,
        geofence_id,
        min_accuracy_m,
        ctx,
        roster_user_id,
    )
    if used_wifi:
        return True
    if not _location_usable_for_rule(location, min_accuracy_m):
        return None
    if counts_inside:
        return True
    return False


def _device_condition_power_labels(
    devices: list[RuleConditionDeviceRefOut],
    ctx: RuleEvaluationContext,
    *,
    fail_fast_unmet: bool = False,
    short_circuit_match: DeviceConditionState | None = None,
) -> tuple[list[str], list[str], list[str]]:
    on_labels: list[str] = []
    off_labels: list[str] = []
    missing_labels: list[str] = []
    for ref in devices:
        label = _device_condition_display_label(ctx, ref)
        is_on = _cached_device_is_on(ctx, ref)
        if is_on is None:
            if ref.family_id in (
                DeviceFamilyId.KASA,
                DeviceFamilyId.SONOS,
                DeviceFamilyId.VIZIO,
            ):
                missing_labels.append(label)
            else:
                missing_labels.append(
                    f"{label} (unsupported family {ref.family_id.value})",
                )
            if fail_fast_unmet:
                break
        elif is_on:
            on_labels.append(label)
            if short_circuit_match == DeviceConditionState.ON:
                break
        else:
            off_labels.append(label)
            if short_circuit_match == DeviceConditionState.OFF or fail_fast_unmet:
                break
    return on_labels, off_labels, missing_labels


def _cached_device_is_open(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
) -> bool | None:
    state = ctx.device_state
    if state is None:
        return None
    match ref.family_id:
        case DeviceFamilyId.TAILWIND:
            return cached_tailwind_is_open(state, ref.device_id)
        case _:
            return None


def _cached_device_matches_state(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
    state: DeviceConditionState,
) -> bool | None:
    """Return whether ``ref`` currently matches ``state``, or None when unknown."""
    if not state.supported_by_family(ref.family_id):
        return None
    match state:
        case DeviceConditionState.ON | DeviceConditionState.PLAYING:
            return _cached_device_is_on(ctx, ref)
        case DeviceConditionState.OFF | DeviceConditionState.PAUSED:
            is_on = _cached_device_is_on(ctx, ref)
            if is_on is None:
                return None
            return not is_on
        case DeviceConditionState.OPEN:
            return _cached_device_is_open(ctx, ref)
        case DeviceConditionState.CLOSED:
            is_open = _cached_device_is_open(ctx, ref)
            if is_open is None:
                return None
            return not is_open
        case DeviceConditionState.OCCUPIED:
            return _cached_device_is_occupied(ctx, ref)
        case DeviceConditionState.CLEAR:
            is_occupied = _cached_device_is_occupied(ctx, ref)
            if is_occupied is None:
                return None
            return not is_occupied


def _complementary_device_state_label(state: DeviceConditionState) -> str:
    """Return the opposite state label used in unmet any/all status details."""
    match state:
        case DeviceConditionState.CLEAR:
            return DeviceConditionState.OCCUPIED.value
        case DeviceConditionState.CLOSED:
            return DeviceConditionState.OPEN.value
        case DeviceConditionState.OCCUPIED:
            return DeviceConditionState.CLEAR.value
        case DeviceConditionState.OFF:
            return DeviceConditionState.ON.value
        case DeviceConditionState.ON:
            return DeviceConditionState.OFF.value
        case DeviceConditionState.OPEN:
            return DeviceConditionState.CLOSED.value
        case DeviceConditionState.PAUSED:
            return DeviceConditionState.PLAYING.value
        case DeviceConditionState.PLAYING:
            return DeviceConditionState.PAUSED.value


def _device_bool_since_for_ref(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
) -> float | None:
    """Return streak start for ``ref`` using backend id when resolvable."""
    backend_id = ref.device_id.strip()
    if ctx.device_state is not None:
        resolved = resolve_device_ref_to_backend_id(
            ctx.device_state,
            family_id=ref.family_id,
            device_ref=backend_id,
        )
        if resolved is not None:
            backend_id = resolved
    return ctx.device_bool_since.get((ref.family_id, backend_id))


def _device_condition_display_label(
    ctx: RuleEvaluationContext,
    ref: RuleConditionDeviceRefOut,
) -> str:
    """Return ``Name (mac)`` using live preferred_label when available, else rule snapshot."""
    device_id = ref.device_id.strip()
    live: str | None = None
    if ctx.device_state is not None:
        live = lookup_preferred_label(
            ctx.device_state,
            family_id=ref.family_id,
            device_id=device_id,
        )
    display_name: str | None = None
    for candidate in (live, ref.display_name):
        if candidate is None:
            continue
        trimmed = candidate.strip()
        if trimmed != "" and trimmed.casefold() != device_id.casefold():
            display_name = trimmed
            break
    return format_device_display(device_id, display_name)


def _device_condition_open_labels(
    devices: list[RuleConditionDeviceRefOut],
    ctx: RuleEvaluationContext,
    *,
    fail_fast_unmet: bool = False,
    short_circuit_match: DeviceConditionState | None = None,
) -> tuple[list[str], list[str], list[str]]:
    open_labels: list[str] = []
    closed_labels: list[str] = []
    missing_labels: list[str] = []
    for ref in devices:
        label = _device_condition_display_label(ctx, ref)
        is_open = _cached_device_is_open(ctx, ref)
        if is_open is None:
            if ref.family_id == DeviceFamilyId.TAILWIND:
                missing_labels.append(label)
            else:
                missing_labels.append(
                    f"{label} (unsupported family {ref.family_id.value})",
                )
            if fail_fast_unmet:
                break
        elif is_open:
            open_labels.append(label)
            if short_circuit_match == DeviceConditionState.OPEN:
                break
        else:
            closed_labels.append(label)
            if fail_fast_unmet:
                break
    return open_labels, closed_labels, missing_labels


def _effective_location_for_rule(
    latest: UserLocationOut | None,
    history: tuple[UserLocationOut, ...] | list[UserLocationOut],
    *,
    min_accuracy_m: int,
    now_epoch: float,
    walkback_max_s: float = LOCATION_HISTORY_WALKBACK_MAX_S,
) -> UserLocationOut | None:
    """Return the newest usable reading within the walkback window.

    ``history`` must be newest-first (as returned by
    ``list_user_location_history_for_walkback``).
    """
    if latest is not None and _location_usable_for_rule(latest, min_accuracy_m):
        return latest
    cutoff = now_epoch - walkback_max_s
    for row in history:
        if _location_reported_at_epoch(row) < cutoff:
            break
        if _location_usable_for_rule(row, min_accuracy_m):
            return row
    return None


def _evaluate_after_local_time(
    condition: AfterLocalTimeCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    target = _parse_hhmm(condition.time_hhmm)
    now_minutes = _local_minutes_from_dt(ctx.now)
    met = target is not None and now_minutes >= target
    display = _format_hhmm_display(condition.time_hhmm)
    if target is None:
        detail = f"Invalid time {condition.time_hhmm}"
    elif met:
        detail = f"Local time is past {display}"
    else:
        detail = f"Waiting until {display}"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=f"After {display}",
        met=met,
    )


def _evaluate_after_sunset(
    condition: AfterSunsetCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    sunset_minutes = _local_minutes_from_iso(ctx.sun.sunset_at, ctx.timezone)
    now_minutes = _local_minutes_from_dt(ctx.now)
    met = _is_in_after_sunset_window(
        now_minutes,
        sunset_minutes,
        condition.offset_minutes,
    )
    sunset_label = _format_iso_local_time(ctx.sun.sunset_at, ctx.timezone)
    if met:
        detail = f"Evening window active (sunset {sunset_label} to midnight)"
    else:
        detail = f"Outside sunset–midnight window (sunset {sunset_label})"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label="After sunset",
        met=met,
    )


def _evaluate_all(
    condition: AllConditionsCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    children = [_evaluate_condition(child, rule, ctx) for child in condition.conditions]
    met = all(child.met for child in children)
    return RuleConditionStatusOut(
        condition=condition,
        detail="All nested conditions met" if met else "Waiting on nested conditions",
        label="All of",
        met=met,
    )


def _evaluate_any(
    condition: AnyConditionsCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    children = [_evaluate_condition(child, rule, ctx) for child in condition.conditions]
    if (
        RuleTrigger.EDGE_TRUE in rule.triggers
        and ctx.triggered_by == RuleEvaluationCause.EDGE
        and _conditions_are_presence_only(condition.conditions)
    ):
        met = False
        presence_details = [child.detail for child in children if child.detail]
        detail = (
            "; ".join(presence_details)
            if presence_details
            else "Fires on geofence enter/leave — see presence per user below"
        )
    else:
        met = any(child.met for child in children)
        detail = "At least one nested condition met" if met else "No nested conditions met yet"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label="Any of",
        met=met,
    )


def _evaluate_before_local_time(
    condition: BeforeLocalTimeCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    target = _parse_hhmm(condition.time_hhmm)
    now_minutes = _local_minutes_from_dt(ctx.now)
    met = target is not None and now_minutes < target
    display = _format_hhmm_display(condition.time_hhmm)
    if target is None:
        detail = f"Invalid time {condition.time_hhmm}"
    elif met:
        detail = f"Local time is before {display}"
    else:
        detail = f"Past {display} for today"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=f"Before {display}",
        met=met,
    )


def _evaluate_before_sunrise(
    condition: BeforeSunriseCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    sunrise_minutes = _local_minutes_from_iso(ctx.sun.sunrise_at, ctx.timezone)
    now_minutes = _local_minutes_from_dt(ctx.now)
    met = _is_in_before_sunrise_window(
        now_minutes,
        sunrise_minutes,
        condition.offset_minutes,
    )
    sunrise_label = _format_iso_local_time(ctx.sun.sunrise_at, ctx.timezone)
    if met:
        detail = f"Morning window active (midnight to sunrise {sunrise_label})"
    else:
        detail = f"Outside midnight–sunrise window (sunrise {sunrise_label})"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label="Before sunrise",
        met=met,
    )


def _evaluate_condition(
    condition: RuleConditionOut,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    if isinstance(condition, AfterLocalTimeCondition):
        return _evaluate_after_local_time(condition, rule, ctx)
    if isinstance(condition, AfterSunsetCondition):
        return _evaluate_after_sunset(condition, rule, ctx)
    if isinstance(condition, AllConditionsCondition):
        return _evaluate_all(condition, rule, ctx)
    if isinstance(condition, AnyConditionsCondition):
        return _evaluate_any(condition, rule, ctx)
    if isinstance(condition, BeforeLocalTimeCondition):
        return _evaluate_before_local_time(condition, rule, ctx)
    if isinstance(condition, BeforeSunriseCondition):
        return _evaluate_before_sunrise(condition, rule, ctx)
    if isinstance(condition, DaylightCondition):
        return _evaluate_daylight(condition, rule, ctx)
    if isinstance(condition, DaysOfWeekCondition):
        return _evaluate_days_of_week(condition, rule, ctx)
    if isinstance(condition, DevicesAllInStateCondition):
        return _evaluate_devices_all_in_state(condition, rule, ctx)
    if isinstance(condition, DevicesAnyInStateCondition):
        return _evaluate_devices_any_in_state(condition, rule, ctx)
    if isinstance(condition, DevicesAnyInStateForSCondition):
        return _evaluate_devices_any_in_state_for_s(condition, rule, ctx)
    if isinstance(condition, LocalTimeWindowCondition):
        return _evaluate_local_time_window(condition, rule, ctx)
    if isinstance(condition, UsersInsideGeofenceCondition):
        return _evaluate_users_geofence(condition, rule, ctx, want_inside=True)
    if isinstance(condition, UsersInsideGeofenceForSCondition):
        return _evaluate_users_inside_geofence_for_s(condition, rule, ctx)
    if isinstance(condition, UsersMinDistanceFromHomeMCondition):
        return _evaluate_users_min_distance_from_home_m(condition, rule, ctx)
    if isinstance(condition, UsersOutsideGeofenceCondition):
        return _evaluate_users_geofence(condition, rule, ctx, want_inside=False)
    if isinstance(condition, UsersOutsideGeofenceForSCondition):
        return _evaluate_users_outside_geofence_for_s(condition, rule, ctx)
    return RuleConditionStatusOut(
        condition=condition,
        detail="Unsupported condition type for status display",
        label="Condition",
        met=False,
    )


def _evaluate_daylight(
    condition: DaylightCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    met = not ctx.sun.is_dark
    sunrise_label = _format_iso_local_time(ctx.sun.sunrise_at, ctx.timezone)
    sunset_label = _format_iso_local_time(ctx.sun.sunset_at, ctx.timezone)
    if met:
        detail = f"Daylight active (sunrise {sunrise_label} to sunset {sunset_label})"
    else:
        detail = f"Outside daylight hours (sunrise {sunrise_label}, sunset {sunset_label})"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label="Daylight",
        met=met,
    )


def _evaluate_days_of_week(
    condition: DaysOfWeekCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    today = ctx.now.weekday()
    # Python weekday(): Mon=0 … Sun=6; rule JSON uses JS getDay(): Sun=0 … Sat=6.
    today_js = (today + 1) % 7
    met = today_js in condition.days
    selected = ", ".join(_DAY_NAMES[day] if 0 <= day < len(_DAY_NAMES) else str(day) for day in sorted(condition.days))
    today_name = _DAY_NAMES[today_js] if 0 <= today_js < len(_DAY_NAMES) else "?"
    if met:
        detail = f"Today ({today_name}) is in {selected}"
    else:
        detail = f"Today ({today_name}) not in {selected}"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label="Days of week",
        met=met,
    )


def _evaluate_devices_all_in_state(
    condition: DevicesAllInStateCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    del rule  # unused; signature matches sibling evaluators
    return _evaluate_devices_in_state(
        condition,
        devices=condition.devices,
        state=condition.state,
        ctx=ctx,
        require_all=True,
    )


def _evaluate_devices_any_in_state(
    condition: DevicesAnyInStateCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    del rule  # unused; signature matches sibling evaluators
    return _evaluate_devices_in_state(
        condition,
        devices=condition.devices,
        state=condition.state,
        ctx=ctx,
        require_all=False,
    )


def _evaluate_devices_any_in_state_for_s(
    condition: DevicesAnyInStateForSCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    del rule  # unused; signature matches sibling evaluators
    label = f"Any device {condition.state.value} {_format_dwell_need_s(condition.min_duration_s)}+"
    if ctx.device_state is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail="discovery not ready",
            label=label,
            met=False,
        )
    now_epoch = ctx.now.timestamp()
    matched_labels: list[str] = []
    pending_labels: list[str] = []
    unmet_labels: list[str] = []
    missing_labels: list[str] = []
    for ref in condition.devices:
        device_label = _device_condition_display_label(ctx, ref)
        matches = _cached_device_matches_state(ctx, ref, condition.state)
        if matches is None:
            if condition.state.supported_by_family(ref.family_id):
                missing_labels.append(device_label)
            else:
                missing_labels.append(
                    f"{device_label} (unsupported family {ref.family_id.value} for state {condition.state.value})",
                )
            continue
        if not matches:
            unmet_labels.append(device_label)
            continue
        since = _device_bool_since_for_ref(ctx, ref)
        if since is None:
            pending_labels.append(f"{device_label} (dwell not started)")
            continue
        elapsed_s = now_epoch - since
        elapsed_label = _format_dwell_elapsed_s(elapsed_s)
        if elapsed_s >= condition.min_duration_s:
            matched_labels.append(f"{device_label} {elapsed_label}")
        else:
            pending_labels.append(
                f"{device_label} {elapsed_label} (need {_format_dwell_need_s(condition.min_duration_s)})",
            )
    if matched_labels:
        detail = f"{condition.state.value.capitalize()}: {', '.join(matched_labels)}"
        if pending_labels or unmet_labels or missing_labels:
            extras: list[str] = []
            if pending_labels:
                extras.append(f"pending: {', '.join(pending_labels)}")
            if unmet_labels:
                extras.append(f"not {condition.state.value}: {', '.join(unmet_labels)}")
            if missing_labels:
                extras.append(f"not found: {', '.join(missing_labels)}")
            detail = f"{detail} ({'; '.join(extras)})"
        met = True
    else:
        met = False
        parts: list[str] = []
        if pending_labels:
            parts.append(", ".join(pending_labels))
        if unmet_labels:
            parts.append(f"not {condition.state.value}: {', '.join(unmet_labels)}")
        if missing_labels:
            parts.append(f"not found: {', '.join(missing_labels)}")
        detail = "; ".join(parts) if parts else f"No device {condition.state.value}"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _evaluate_devices_in_state(
    condition: RuleConditionOut,
    *,
    devices: list[RuleConditionDeviceRefOut],
    state: DeviceConditionState,
    ctx: RuleEvaluationContext,
    require_all: bool,
) -> RuleConditionStatusOut:
    """Shared instant device-state evaluator for any/all in-state conditions."""
    label = f"All devices {state.value}" if require_all else f"Any device {state.value}"
    if ctx.device_state is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail="discovery not ready",
            label=label,
            met=False,
        )
    matched_labels: list[str] = []
    unmet_labels: list[str] = []
    missing_labels: list[str] = []
    for ref in devices:
        device_label = _device_condition_display_label(ctx, ref)
        matches = _cached_device_matches_state(ctx, ref, state)
        if matches is None:
            if state.supported_by_family(ref.family_id):
                missing_labels.append(device_label)
            else:
                missing_labels.append(
                    f"{device_label} (unsupported family {ref.family_id.value} for state {state.value})",
                )
            if require_all:
                break
            continue
        if matches:
            matched_labels.append(device_label)
            if not require_all:
                break
        else:
            unmet_labels.append(device_label)
            if require_all:
                break
    if require_all:
        if missing_labels:
            met = False
            detail = f"Not found: {', '.join(missing_labels)}"
        elif unmet_labels:
            met = False
            complement = _complementary_device_state_label(state)
            detail = f"{complement.capitalize()}: {', '.join(unmet_labels)}"
        else:
            met = True
            detail = f"All {state.value} ({', '.join(matched_labels)})"
    elif matched_labels:
        met = True
        detail = f"{state.value.capitalize()}: {', '.join(matched_labels)}"
        if missing_labels:
            detail = f"{detail} (not found: {', '.join(missing_labels)})"
    elif unmet_labels:
        met = False
        complement = _complementary_device_state_label(state)
        if missing_labels:
            detail = (
                f"All resolved devices {complement} ({', '.join(unmet_labels)}); not found: {', '.join(missing_labels)}"
            )
        else:
            detail = f"All {complement} ({', '.join(unmet_labels)})"
    else:
        met = False
        detail = f"Not found: {', '.join(missing_labels)}"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _evaluate_local_time_window(
    condition: LocalTimeWindowCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    met = _is_in_local_time_window(
        condition.start_hhmm,
        condition.end_hhmm,
        ctx.now,
    )
    window_label = _format_window_display(
        condition.start_hhmm,
        condition.end_hhmm,
    )
    if met is None:
        detail = f"Invalid window {condition.start_hhmm}–{condition.end_hhmm}"
        met_value = False
    elif met:
        detail = f"Local time is within {window_label}"
        met_value = True
    else:
        detail = f"Waiting for clock window {window_label}"
        met_value = False
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=f"Clock window {window_label}",
        met=met_value,
    )


def _evaluate_users_geofence(
    condition: UsersInsideGeofenceCondition | UsersOutsideGeofenceCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
    *,
    want_inside: bool,
) -> RuleConditionStatusOut:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    fence_label = geofence.label if geofence is not None else condition.geofence_id
    if geofence is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail=f'Unknown geofence "{condition.geofence_id}"',
            label="Geofence",
            met=False,
        )

    min_accuracy_m = rule.min_location_accuracy_m
    settings = load_settings_location()
    presence_lines: list[str] = []
    unmet_names: list[str] = []
    ignored_accuracy: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            presence_lines.append(
                f'"{rule_user_id}": not in user roster (sync users from My Tracks)',
            )
            unmet_names.append(rule_user_id)
            continue
        location = ctx.user_locations.get(roster_user_id)
        name = _user_display_name(ctx, roster_user_id)
        if location is None:
            presence_lines.append(f"{name}: no location yet")
            unmet_names.append(name)
            continue
        effective = _resolved_location_for_geofence_rule(
            location,
            ctx.user_location_history.get(roster_user_id, ()),
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=ctx.now.timestamp(),
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        if effective is None:
            if not _location_usable_for_rule(location, min_accuracy_m):
                ignored_accuracy.append(
                    f"{name} (±{location.accuracy_m if location.accuracy_m is not None else '?'} m "
                    f"> {min_accuracy_m} m threshold)",
                )
            presence_lines.append(f"{name}: location ignored (low accuracy)")
            unmet_names.append(name)
            continue
        if _wifi_home_presence_applies_for_location(
            settings,
            condition.geofence_id,
            effective,
            geofences=ctx.geofences,
            min_accuracy_m=min_accuracy_m,
            ctx=ctx,
            roster_user_id=roster_user_id,
        ):
            _log_wifi_home_presence_overrode_low_accuracy(
                roster_user_id,
                condition.geofence_id,
                accuracy_m=effective.accuracy_m,
                threshold_m=min_accuracy_m,
            )
            presence_lines.append(f"{name} is inside {fence_label} (WiFi home presence)")
            if not want_inside:
                unmet_names.append(name)
            continue
        inside = _user_inside_geofence(effective, geofence, min_accuracy_m)
        if inside:
            presence_lines.append(f"{name} is inside {fence_label}")
        else:
            presence_lines.append(f"{name} is outside {fence_label}")
        if want_inside and not inside:
            unmet_names.append(name)
        if not want_inside and inside:
            unmet_names.append(name)

    selected_names: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            selected_names.append(rule_user_id)
            continue
        selected_names.append(_user_display_name(ctx, roster_user_id))
    who = _join_names(selected_names)
    if RuleTrigger.EDGE_TRUE in rule.triggers and ctx.triggered_by == RuleEvaluationCause.EDGE:
        label = f"Presence at {fence_label} ({who})" if want_inside else f"Outside {fence_label} ({who})"
        met = False
        if ignored_accuracy:
            detail = f"Ignored low-accuracy location: {'; '.join(ignored_accuracy)}"
        else:
            detail = "; ".join(presence_lines)
    else:
        met = len(unmet_names) == 0
        label = f"When {who} enter {fence_label}" if want_inside else f"When {who} leave {fence_label}"
        if met:
            detail = f"Everyone is inside {fence_label}" if want_inside else f"Everyone is outside {fence_label}"
        elif ignored_accuracy:
            detail = f"Ignored low-accuracy location: {'; '.join(ignored_accuracy)}"
        elif want_inside:
            detail = f"Waiting for {', '.join(unmet_names)} to enter {fence_label}"
        else:
            detail = f"Waiting for {', '.join(unmet_names)} to leave {fence_label}"
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _evaluate_users_inside_geofence_for_s(
    condition: UsersInsideGeofenceForSCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    fence_label = geofence.label if geofence is not None else condition.geofence_id
    if geofence is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail=f'Unknown geofence "{condition.geofence_id}"',
            label="Geofence dwell",
            met=False,
        )

    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    need_label = _format_dwell_need_s(condition.min_inside_s)
    presence_lines: list[str] = []
    unmet = False
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            presence_lines.append(
                f'"{rule_user_id}": not in user roster (sync users from My Tracks)',
            )
            unmet = True
            continue
        location = ctx.user_locations.get(roster_user_id)
        name = _user_display_name(ctx, roster_user_id)
        if location is None:
            presence_lines.append(f"{name}: no location yet")
            unmet = True
            continue
        history = ctx.user_location_history.get(roster_user_id, ())
        effective = _resolved_location_for_geofence_rule(
            location,
            history,
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        inside_since = ctx.geofence_inside_since.get(
            (roster_user_id, condition.geofence_id),
        )
        if inside_since is not None:
            inside_s = now_epoch - inside_since
            elapsed_label = _format_dwell_elapsed_s(inside_s)
            accurate_inside = _accurate_inside_from_location(
                effective,
                geofence,
                condition.geofence_id,
                min_accuracy_m,
                ctx,
                roster_user_id,
            )
            if inside_s >= condition.min_inside_s and accurate_inside is not False:
                presence_lines.append(
                    f"{name} inside {elapsed_label} (need {need_label})",
                )
                continue
            if inside_s >= condition.min_inside_s and accurate_inside is False:
                presence_lines.append(f"{name} outside")
                unmet = True
                continue
        counts_inside, used_wifi = _user_counts_inside_geofence_for_rule(
            effective or location,
            geofence,
            condition.geofence_id,
            min_accuracy_m,
            ctx,
            roster_user_id,
        )
        if used_wifi:
            _log_wifi_home_presence_overrode_low_accuracy(
                roster_user_id,
                condition.geofence_id,
                accuracy_m=(effective or location).accuracy_m,
                threshold_m=min_accuracy_m,
            )
        if not counts_inside:
            if inside_since is not None:
                inside_s = now_epoch - inside_since
                elapsed_label = _format_dwell_elapsed_s(inside_s)
                if not _location_usable_for_rule(location, min_accuracy_m):
                    presence_lines.append(f"{name}: location ignored (low accuracy)")
                else:
                    presence_lines.append(f"{name} outside")
                if inside_s < condition.min_inside_s:
                    unmet = True
                continue
            if not _location_usable_for_rule(location, min_accuracy_m):
                presence_lines.append(f"{name}: location ignored (low accuracy)")
            else:
                presence_lines.append(f"{name} outside")
            unmet = True
            continue
        if used_wifi:
            presence_lines.append(
                f"{name} is inside {fence_label} (WiFi home presence)",
            )
        if inside_since is None:
            presence_lines.append(f"{name} inside (dwell not started)")
            unmet = True
            continue
        inside_s = now_epoch - inside_since
        elapsed_label = _format_dwell_elapsed_s(inside_s)
        if used_wifi:
            presence_lines.append(
                f"{name} inside {elapsed_label} via WiFi home presence (need {need_label})",
            )
        else:
            presence_lines.append(
                f"{name} inside {elapsed_label} (need {need_label})",
            )
        if inside_s < condition.min_inside_s:
            unmet = True

    selected_names: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            selected_names.append(rule_user_id)
            continue
        selected_names.append(_user_display_name(ctx, roster_user_id))
    who = _join_names(selected_names)
    label = f"Inside {fence_label} {need_label}+ ({who})"
    if unmet:
        detail = "; ".join(presence_lines)
        met = False
    else:
        detail = f"Everyone inside {fence_label} for at least {need_label}"
        met = True
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _evaluate_users_min_distance_from_home_m(
    condition: UsersMinDistanceFromHomeMCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    need_label = _format_distance_m(condition.min_distance_m)
    home = try_resolve_home_location(load_settings_location())
    if home is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail="Home location is not configured (settings_location lat/lon)",
            label=f"At least {need_label} from home",
            met=False,
        )

    home_name = home.home_label or "home"
    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    presence_lines: list[str] = []
    unmet_names: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            presence_lines.append(
                f'"{rule_user_id}": not in user roster (sync users from My Tracks)',
            )
            unmet_names.append(rule_user_id)
            continue
        name = _user_display_name(ctx, roster_user_id)
        latest = ctx.user_locations.get(roster_user_id)
        if latest is None:
            presence_lines.append(f"{name}: no location yet")
            unmet_names.append(name)
            continue
        effective = _effective_location_for_rule(
            latest,
            ctx.user_location_history.get(roster_user_id, ()),
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            walkback_max_s=ctx.walkback_max_s,
        )
        if effective is None:
            accuracy = latest.accuracy_m
            accuracy_label = f"±{accuracy} m" if accuracy is not None else "unknown accuracy"
            presence_lines.append(
                f"{name}: location ignored ({accuracy_label} > {min_accuracy_m} m threshold or stale)",
            )
            unmet_names.append(name)
            continue
        distance_m = _haversine_m(
            effective.lat,
            effective.lon,
            home.lat,
            home.lon,
        )
        distance_label = _format_distance_m(distance_m)
        if distance_m >= condition.min_distance_m:
            presence_lines.append(
                f"{name} is {distance_label} from {home_name} (≥ {need_label})",
            )
            continue
        presence_lines.append(
            f"{name} is {distance_label} from {home_name} (need ≥ {need_label})",
        )
        unmet_names.append(name)

    selected_names: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            selected_names.append(rule_user_id)
            continue
        selected_names.append(_user_display_name(ctx, roster_user_id))
    who = _join_names(selected_names)
    label = f"{who} ≥ {need_label} from {home_name}"
    met = users_min_distance_from_home_met(
        ctx=ctx,
        min_distance_m=condition.min_distance_m,
        min_location_accuracy_m=min_accuracy_m,
        user_ids=condition.user_ids,
    )
    if met:
        detail = f"Everyone is at least {need_label} from {home_name}"
    else:
        detail = "; ".join(presence_lines)
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _evaluate_users_outside_geofence_for_s(
    condition: UsersOutsideGeofenceForSCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> RuleConditionStatusOut:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    fence_label = geofence.label if geofence is not None else condition.geofence_id
    if geofence is None:
        return RuleConditionStatusOut(
            condition=condition,
            detail=f'Unknown geofence "{condition.geofence_id}"',
            label="Geofence away dwell",
            met=False,
        )

    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    need_label = _format_dwell_need_s(condition.min_outside_s)
    presence_lines: list[str] = []
    unmet = False
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            presence_lines.append(
                f'"{rule_user_id}": not in user roster (sync users from My Tracks)',
            )
            unmet = True
            continue
        location = ctx.user_locations.get(roster_user_id)
        name = _user_display_name(ctx, roster_user_id)
        if location is None:
            presence_lines.append(f"{name}: no location yet")
            unmet = True
            continue
        history = ctx.user_location_history.get(roster_user_id, ())
        effective = _resolved_location_for_geofence_rule(
            location,
            history,
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        outside_since = ctx.geofence_outside_since.get(
            (roster_user_id, condition.geofence_id),
        )
        if outside_since is not None:
            outside_s = now_epoch - outside_since
            elapsed_label = _format_dwell_elapsed_s(outside_s)
            accurate_inside = _accurate_inside_from_location(
                effective,
                geofence,
                condition.geofence_id,
                min_accuracy_m,
                ctx,
                roster_user_id,
            )
            if outside_s >= condition.min_outside_s and accurate_inside is not True:
                presence_lines.append(
                    f"{name} outside {elapsed_label} (need {need_label})",
                )
                continue
        counts_inside, used_wifi = _user_counts_inside_geofence_for_rule(
            effective or location,
            geofence,
            condition.geofence_id,
            min_accuracy_m,
            ctx,
            roster_user_id,
        )
        if counts_inside:
            if used_wifi:
                presence_lines.append(
                    f"{name} is inside {fence_label} (WiFi home presence)",
                )
            else:
                presence_lines.append(f"{name} inside {fence_label}")
            unmet = True
            continue
        if outside_since is None:
            if not _location_usable_for_rule(location, min_accuracy_m):
                presence_lines.append(f"{name}: location ignored (low accuracy)")
            else:
                presence_lines.append(f"{name} outside (dwell not started)")
            unmet = True
            continue
        outside_s = now_epoch - outside_since
        elapsed_label = _format_dwell_elapsed_s(outside_s)
        if not _location_usable_for_rule(location, min_accuracy_m):
            presence_lines.append(f"{name}: location ignored (low accuracy)")
        else:
            presence_lines.append(
                f"{name} outside {elapsed_label} (need {need_label})",
            )
        if outside_s < condition.min_outside_s:
            unmet = True

    selected_names: list[str] = []
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            selected_names.append(rule_user_id)
            continue
        selected_names.append(_user_display_name(ctx, roster_user_id))
    who = _join_names(selected_names)
    label = f"Outside {fence_label} {need_label}+ ({who})"
    if unmet:
        detail = "; ".join(presence_lines)
        met = False
    else:
        detail = f"Everyone outside {fence_label} for at least {need_label}"
        met = True
    return RuleConditionStatusOut(
        condition=condition,
        detail=detail,
        label=label,
        met=met,
    )


def _format_distance_m(distance_m: float) -> str:
    if distance_m >= 1000.0:
        km = distance_m / 1000.0
        if abs(km - round(km)) < 1e-6:
            return f"{int(round(km))} km"
        return f"{km:.1f} km"
    if abs(distance_m - round(distance_m)) < 1e-6:
        return f"{int(round(distance_m))} m"
    return f"{distance_m:.0f} m"


def _format_dwell_duration_s(total_s: float | int) -> str:
    whole = max(0, int(total_s))
    if whole < 60:
        return f"{whole} sec"
    minutes = whole // 60
    seconds = whole % 60
    if seconds == 0:
        return f"{minutes} min"
    return f"{minutes} min {seconds} sec"


def _format_dwell_elapsed_s(elapsed_s: float) -> str:
    return _format_dwell_duration_s(elapsed_s)


def _format_dwell_need_s(min_inside_s: int) -> str:
    return _format_dwell_duration_s(min_inside_s)


def _format_hhmm_display(hhmm: str) -> str:
    parsed = _parse_hhmm(hhmm)
    if parsed is None:
        return hhmm
    hour = parsed // 60
    minute = parsed % 60
    display_hour = hour % 12 or 12
    suffix = "AM" if hour < 12 else "PM"
    return f"{display_hour}:{minute:02d} {suffix}"


def _format_iso_local_time(iso: str, tz: ZoneInfo) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    display_hour = dt.hour % 12 or 12
    suffix = "AM" if dt.hour < 12 else "PM"
    return f"{display_hour}:{dt.minute:02d} {suffix}"


def _format_window_display(start_hhmm: str, end_hhmm: str) -> str:
    return f"{_format_hhmm_display(start_hhmm)} – {_format_hhmm_display(end_hhmm)}"


def _location_reported_at_epoch(location: UserLocationOut) -> float:
    return datetime.fromisoformat(
        location.reported_at.replace("Z", "+00:00"),
    ).timestamp()


def _location_usable_for_rule(
    location: UserLocationOut | None,
    min_accuracy_m: int,
) -> bool:
    if location is None:
        return False
    if location.accuracy_m is not None and location.accuracy_m > min_accuracy_m:
        return False
    return True


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_m = 6_371_000.0
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * earth_radius_m * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_in_after_sunset_window(
    now_minutes: int,
    sunset_minutes: int,
    offset_minutes: int,
) -> bool:
    start = sunset_minutes + offset_minutes
    if start >= MINUTES_PER_DAY:
        return False
    return now_minutes >= start and now_minutes < MINUTES_PER_DAY


def _is_in_before_sunrise_window(
    now_minutes: int,
    sunrise_minutes: int,
    offset_minutes: int,
) -> bool:
    end = sunrise_minutes + offset_minutes
    return now_minutes >= 0 and now_minutes < end


def desired_bool_for_device_condition_state(state: DeviceConditionState) -> bool:
    """Return the natural cached bool that means ``state`` is currently true."""
    return state.desired_bool()


def iter_device_dwell_for_s_conditions(
    conditions: list[RuleConditionOut],
) -> list[DevicesAnyInStateForSCondition]:
    found: list[DevicesAnyInStateForSCondition] = []
    for condition in conditions:
        if isinstance(condition, DevicesAnyInStateForSCondition):
            found.append(condition)
        elif isinstance(condition, AllConditionsCondition):
            found.extend(iter_device_dwell_for_s_conditions(condition.conditions))
        elif isinstance(condition, AnyConditionsCondition):
            found.extend(iter_device_dwell_for_s_conditions(condition.conditions))
    return found


def iter_dwell_for_s_conditions(
    conditions: list[RuleConditionOut],
) -> list[UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition]:
    found: list[UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition] = []
    for condition in conditions:
        if isinstance(
            condition,
            UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition,
        ):
            found.append(condition)
        elif isinstance(condition, AllConditionsCondition):
            found.extend(iter_dwell_for_s_conditions(condition.conditions))
        elif isinstance(condition, AnyConditionsCondition):
            found.extend(iter_dwell_for_s_conditions(condition.conditions))
    return found


def natural_bool_for_device_family(
    ctx: RuleEvaluationContext,
    *,
    family_id: DeviceFamilyId,
    device_id: str,
) -> bool | None:
    """Return the family's natural cached bool (on/open/playing) for ``device_id``."""
    ref = RuleConditionDeviceRefOut(device_id=device_id, family_id=family_id)
    match family_id:
        case DeviceFamilyId.TAILWIND:
            return _cached_device_is_open(ctx, ref)
        case DeviceFamilyId.KASA | DeviceFamilyId.SONOS | DeviceFamilyId.VIZIO:
            return _cached_device_is_on(ctx, ref)
        case _:
            return None


def _is_in_local_time_window(
    start_hhmm: str,
    end_hhmm: str,
    now: datetime,
) -> bool | None:
    start = _parse_hhmm(start_hhmm)
    end = _parse_hhmm(end_hhmm)
    if start is None or end is None:
        return None
    now_minutes = _local_minutes_from_dt(now)
    if start <= end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def _join_names(names: list[str]) -> str:
    if not names:
        return "nobody"
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return f"{', '.join(names[:-1])}, and {names[-1]}"


def _local_minutes_from_dt(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _local_minutes_from_iso(iso: str, tz: ZoneInfo) -> int:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(tz)
    return _local_minutes_from_dt(dt)


def _log_wifi_home_presence_overrode_low_accuracy(
    user_id: str,
    geofence_id: str,
    *,
    accuracy_m: int | None,
    threshold_m: int,
) -> None:
    _LOGGER.info(
        "[rules] wifi home presence overrode low-accuracy location "
        "user_id=%s geofence_id=%s accuracy_m=%s threshold_m=%s",
        user_id,
        geofence_id,
        accuracy_m,
        threshold_m,
    )


def _parse_hhmm(hhmm: str) -> int | None:
    match = _HHMM_RE.match(hhmm.strip())
    if match is None:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _presence_user_ids_for_condition(
    condition: RuleConditionOut,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    if isinstance(condition, AllConditionsCondition):
        ids: set[str] = set()
        for child in condition.conditions:
            ids.update(_presence_user_ids_for_condition(child, rule, ctx))
        return ids
    if isinstance(condition, AnyConditionsCondition):
        ids: set[str] = set()
        for child in condition.conditions:
            child_ids = _presence_user_ids_for_condition(child, rule, ctx)
            if child_ids:
                ids.update(child_ids)
        return ids
    if isinstance(condition, UsersInsideGeofenceCondition):
        return _roster_user_ids_satisfying_users_inside_geofence(
            condition,
            rule,
            ctx,
        )
    if isinstance(condition, UsersInsideGeofenceForSCondition):
        return _roster_user_ids_satisfying_users_inside_geofence_for_s(
            condition,
            rule,
            ctx,
        )
    if isinstance(condition, UsersMinDistanceFromHomeMCondition):
        return _roster_user_ids_satisfying_users_min_distance_from_home_m(
            condition,
            rule,
            ctx,
        )
    if isinstance(condition, UsersOutsideGeofenceCondition):
        return _roster_user_ids_satisfying_users_outside_geofence(
            condition,
            rule,
            ctx,
        )
    if isinstance(condition, UsersOutsideGeofenceForSCondition):
        return _roster_user_ids_satisfying_users_outside_geofence_for_s(
            condition,
            rule,
            ctx,
        )
    if isinstance(
        condition,
        (
            AfterLocalTimeCondition,
            AfterSunsetCondition,
            BeforeLocalTimeCondition,
            BeforeSunriseCondition,
            DaysOfWeekCondition,
            DevicesAllInStateCondition,
            DevicesAnyInStateCondition,
            DevicesAnyInStateForSCondition,
            LocalTimeWindowCondition,
        ),
    ):
        return set()
    _LOGGER.error(
        "[rules] unhandled condition type %s in presence_user_ids_for_condition",
        type(condition).__name__,
    )
    raise AssertionError(f"Unhandled condition type {type(condition).__name__!r} in presence_user_ids_for_condition")


def _resolved_location_for_geofence_rule(
    latest: UserLocationOut | None,
    history: tuple[UserLocationOut, ...] | list[UserLocationOut],
    *,
    geofence_id: str,
    min_accuracy_m: int,
    now_epoch: float,
    ctx: RuleEvaluationContext,
    roster_user_id: str,
) -> UserLocationOut | None:
    """Prefer a raw WiFi-home reading, then walk back for the last usable GPS location."""
    if latest is not None:
        settings = load_settings_location()
        if _wifi_home_presence_applies_for_location(
            settings,
            geofence_id,
            latest,
            geofences=ctx.geofences,
            min_accuracy_m=min_accuracy_m,
            ctx=ctx,
            roster_user_id=roster_user_id,
        ):
            return latest
    return _effective_location_for_rule(
        latest,
        history,
        min_accuracy_m=min_accuracy_m,
        now_epoch=now_epoch,
        walkback_max_s=ctx.walkback_max_s,
    )


def _roster_user_ids_satisfying_users_inside_geofence(
    condition: UsersInsideGeofenceCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    if geofence is None:
        return set()
    min_accuracy_m = rule.min_location_accuracy_m
    satisfied: set[str] = set()
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            continue
        location = ctx.user_locations.get(roster_user_id)
        if location is None:
            continue
        effective = _resolved_location_for_geofence_rule(
            location,
            ctx.user_location_history.get(roster_user_id, ()),
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=ctx.now.timestamp(),
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        if effective is None:
            continue
        counts_inside, _used_wifi = _user_counts_inside_geofence_for_rule(
            effective,
            geofence,
            condition.geofence_id,
            min_accuracy_m,
            ctx,
            roster_user_id,
        )
        if counts_inside:
            satisfied.add(roster_user_id)
    return satisfied


def _roster_user_ids_satisfying_users_inside_geofence_for_s(
    condition: UsersInsideGeofenceForSCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    if geofence is None:
        return set()
    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    satisfied: set[str] = set()
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            continue
        location = ctx.user_locations.get(roster_user_id)
        if location is None:
            continue
        history = ctx.user_location_history.get(roster_user_id, ())
        effective = _resolved_location_for_geofence_rule(
            location,
            history,
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        inside_since = ctx.geofence_inside_since.get(
            (roster_user_id, condition.geofence_id),
        )
        if inside_since is not None:
            inside_s = now_epoch - inside_since
            if inside_s >= condition.min_inside_s:
                accurate_inside = _accurate_inside_from_location(
                    effective,
                    geofence,
                    condition.geofence_id,
                    min_accuracy_m,
                    ctx,
                    roster_user_id,
                )
                if accurate_inside is not False:
                    satisfied.add(roster_user_id)
                    continue
        location_eval = effective or location
        counts_inside, _used_wifi = _user_counts_inside_geofence_for_rule(
            location_eval,
            geofence,
            condition.geofence_id,
            min_accuracy_m,
            ctx,
            roster_user_id,
        )
        if not counts_inside:
            continue
        if inside_since is None:
            continue
        if now_epoch - inside_since < condition.min_inside_s:
            continue
        satisfied.add(roster_user_id)
    return satisfied


def _roster_user_ids_satisfying_users_min_distance_from_home_m(
    condition: UsersMinDistanceFromHomeMCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    home = try_resolve_home_location(load_settings_location())
    if home is None:
        return set()
    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    satisfied: set[str] = set()
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            continue
        latest = ctx.user_locations.get(roster_user_id)
        if latest is None:
            continue
        effective = _effective_location_for_rule(
            latest,
            ctx.user_location_history.get(roster_user_id, ()),
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            walkback_max_s=ctx.walkback_max_s,
        )
        if effective is None:
            continue
        distance_m = _haversine_m(
            effective.lat,
            effective.lon,
            home.lat,
            home.lon,
        )
        if distance_m >= condition.min_distance_m:
            satisfied.add(roster_user_id)
    return satisfied


def _roster_user_ids_satisfying_users_outside_geofence(
    condition: UsersOutsideGeofenceCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    if geofence is None:
        return set()
    settings = load_settings_location()
    min_accuracy_m = rule.min_location_accuracy_m
    satisfied: set[str] = set()
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            continue
        location = ctx.user_locations.get(roster_user_id)
        if location is None:
            continue
        effective = _resolved_location_for_geofence_rule(
            location,
            ctx.user_location_history.get(roster_user_id, ()),
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=ctx.now.timestamp(),
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        if effective is None:
            continue
        if _wifi_home_presence_applies_for_location(
            settings,
            condition.geofence_id,
            effective,
            geofences=ctx.geofences,
            min_accuracy_m=min_accuracy_m,
            ctx=ctx,
            roster_user_id=roster_user_id,
        ):
            continue
        if not _location_usable_for_rule(effective, min_accuracy_m):
            continue
        if not _user_inside_geofence(effective, geofence, min_accuracy_m):
            satisfied.add(roster_user_id)
    return satisfied


def _roster_user_ids_satisfying_users_outside_geofence_for_s(
    condition: UsersOutsideGeofenceForSCondition,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> set[str]:
    geofence = next(
        (row for row in ctx.geofences if row.geofence_id == condition.geofence_id),
        None,
    )
    if geofence is None:
        return set()
    min_accuracy_m = rule.min_location_accuracy_m
    now_epoch = ctx.now.timestamp()
    satisfied: set[str] = set()
    for rule_user_id in condition.user_ids:
        roster_user_id = ctx.resolve_user_id(rule_user_id)
        if roster_user_id is None:
            continue
        location = ctx.user_locations.get(roster_user_id)
        if location is None:
            continue
        history = ctx.user_location_history.get(roster_user_id, ())
        effective = _resolved_location_for_geofence_rule(
            location,
            history,
            geofence_id=condition.geofence_id,
            min_accuracy_m=min_accuracy_m,
            now_epoch=now_epoch,
            ctx=ctx,
            roster_user_id=roster_user_id,
        )
        outside_since = ctx.geofence_outside_since.get(
            (roster_user_id, condition.geofence_id),
        )
        if outside_since is not None:
            outside_s = now_epoch - outside_since
            if outside_s >= condition.min_outside_s:
                accurate_inside = _accurate_inside_from_location(
                    effective,
                    geofence,
                    condition.geofence_id,
                    min_accuracy_m,
                    ctx,
                    roster_user_id,
                )
                if accurate_inside is not True:
                    satisfied.add(roster_user_id)
                    continue
        location_eval = effective or location
        counts_inside, _used_wifi = _user_counts_inside_geofence_for_rule(
            location_eval,
            geofence,
            condition.geofence_id,
            min_accuracy_m,
            ctx,
            roster_user_id,
        )
        if counts_inside:
            continue
        if not _location_usable_for_rule(location_eval, min_accuracy_m):
            continue
        if outside_since is None:
            continue
        if now_epoch - outside_since < condition.min_outside_s:
            continue
        satisfied.add(roster_user_id)
    return satisfied


def _user_counts_inside_geofence_for_rule(
    location: UserLocationOut,
    geofence: GeofenceOut,
    geofence_id: str,
    min_accuracy_m: int,
    ctx: RuleEvaluationContext,
    roster_user_id: str,
) -> tuple[bool, bool]:
    """Return whether a user counts as inside and whether WiFi overrode low GPS."""
    settings = load_settings_location()
    if _wifi_home_presence_applies_for_location(
        settings,
        geofence_id,
        location,
        geofences=ctx.geofences,
        min_accuracy_m=min_accuracy_m,
        ctx=ctx,
        roster_user_id=roster_user_id,
    ):
        return True, True
    if not _location_usable_for_rule(location, min_accuracy_m):
        return False, False
    return _user_inside_geofence(location, geofence, min_accuracy_m), False


def _user_display_name(
    ctx: RuleEvaluationContext,
    user_id: str,
) -> str:
    display_name = ctx.user_display_names.get(user_id)
    trimmed = (display_name or "").strip()
    if trimmed:
        return trimmed
    return user_id


def _user_inside_geofence(
    location: UserLocationOut | None,
    geofence: GeofenceOut,
    min_accuracy_m: int,
) -> bool:
    if not _location_usable_for_rule(location, min_accuracy_m):
        return False
    if not geofence.enabled or location is None:
        return False
    distance_m = _haversine_m(
        location.lat,
        location.lon,
        geofence.center_lat,
        geofence.center_lon,
    )
    return distance_m <= geofence.radius_m


def _wifi_home_presence_applies_for_location(
    settings: SettingsLocationOut,
    geofence_id: str,
    location: UserLocationOut,
    *,
    geofences: tuple[GeofenceOut, ...],
    min_accuracy_m: int,
    ctx: RuleEvaluationContext,
    roster_user_id: str,
) -> bool:
    return wifi_home_presence_applies(
        settings,
        geofence_id,
        location.connection_type,
        accuracy_m=location.accuracy_m,
        geofences=geofences,
        lat=location.lat,
        lon=location.lon,
        min_accuracy_m=min_accuracy_m,
        home_wifi_bssid=ctx.user_home_wifi_bssid.get(roster_user_id),
        observed_wifi_bssid=location.wifi_bssid,
    )


def _to_iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def presence_user_ids_for_rule(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> tuple[str, ...]:
    """Return roster user ids whose geofence presence currently satisfies the rule."""
    ids: set[str] = set()
    for condition in rule.conditions.all:
        ids.update(_presence_user_ids_for_condition(condition, rule, ctx))
    return tuple(sorted(ids))


def dwell_episode_blocks_fire(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> bool:
    """Return True when every dwell user already fired for the current presence episode."""
    if (
        RuleTrigger.DEVICE_STATE not in rule.triggers
        and RuleTrigger.DWELL_SATISFIED not in rule.triggers
        and RuleTrigger.SCHEDULED not in rule.triggers
    ):
        return False
    dwell_conditions = iter_dwell_for_s_conditions(rule.conditions.all)
    if not dwell_conditions:
        return False
    checked_any = False
    for condition in dwell_conditions:
        if isinstance(condition, UsersOutsideGeofenceForSCondition):
            consumed = ctx.scheduled_outside_dwell_consumed_episode
        else:
            consumed = ctx.scheduled_inside_dwell_consumed_episode
        for rule_user_id in condition.user_ids:
            roster_user_id = ctx.resolve_user_id(rule_user_id)
            if roster_user_id is None:
                continue
            checked_any = True
            episode = ctx.geofence_presence_episode.get(
                (roster_user_id, condition.geofence_id),
                0,
            )
            if consumed.get((rule.id, roster_user_id, condition.geofence_id)) != episode:
                return False
    return checked_any
