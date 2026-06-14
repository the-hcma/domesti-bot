"""Asyncio automation rule evaluator on location ingest."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AllConditionsCondition,
    AnyConditionsCondition,
    RuleConditionOut,
    RuleOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersOutsideGeofenceCondition,
)
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.domesti_bot_cli import DeviceManagersState
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_locations,
)
from app.rule_actions import (
    RuleActionDispatchError,
    dispatch_rule_device_actions,
    send_rule_notification_email,
)
from app.rule_conditions import (
    RuleEvaluationContext,
    RuleEvaluationResult,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rule_fire_state_store import list_rule_fire_states, upsert_rule_fire_state
from app.rule_validation import (
    build_roster_user_id_lookup,
    collect_rule_user_ids,
    rule_references_user_id,
)
from app.rules_store import GeofenceRecord, list_geofences, list_users

_LOGGER = logging.getLogger(__name__)
_MIN_GEOFENCE_OUTSIDE_DWELL_S = 300.0
_RULE_EVALUATOR_TICK_S = 60.0
DeferredGeofenceEvent = Literal["entered", "left"]
RuleFireSource = Literal["deferred", "immediate"]


@dataclass(frozen=True)
class _DeferredAccuracyEdge:
    event: DeferredGeofenceEvent
    expires_at: float
    geofence_id: str
    observed_at: float
    rule_id: str
    user_id: str


@dataclass(frozen=True)
class GeofenceTransition:
    entered: bool = False
    left: bool = False


@dataclass(frozen=True)
class RuleEvaluatorFireState:
    last_error: str | None = None
    last_fired_at: float | None = None


@dataclass
class _RuleRuntimeState:
    last_error: str | None = None
    last_fired_at: float | None = None


class RuleEvaluator:
    """Evaluate file-backed rules on location ingest with geofence edge semantics."""

    def __init__(
        self,
        *,
        cache_path: Path | None,
        device_state_getter: Callable[[], DeviceManagersState | None],
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._device_state_getter = device_state_getter
        self._now_fn = now_fn or time.time
        # Outside-since timestamps are in-memory only; a restart clears them so the
        # first geofence enter after a bounce is never falsely debounced.
        self._deferred_accuracy_edges: dict[
            tuple[str, str, str, str],
            _DeferredAccuracyEdge,
        ] = {}
        self._geofence_outside_since: dict[tuple[str, str], float] = {}
        self._geofence_was_inside: dict[tuple[str, str], bool] = {}
        self._last_run_at: float | None = None
        self._next_sun_check_at: float | None = None
        self._process_lock = asyncio.Lock()
        self._rule_state: dict[str, _RuleRuntimeState] = {}
        self._stop = asyncio.Event()
        self._tick_task: asyncio.Task[None] | None = None
        self._load_persisted_rule_state()
        self._seed_geofence_state()

    async def close(self) -> None:
        self._stop.set()
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task

    def fire_state_for_rule(self, rule_id: str) -> RuleEvaluatorFireState:
        state = self._rule_state.get(rule_id)
        if state is None:
            return RuleEvaluatorFireState()
        return RuleEvaluatorFireState(
            last_error=state.last_error,
            last_fired_at=state.last_fired_at,
        )

    @property
    def last_run_at(self) -> float | None:
        return self._last_run_at

    @property
    def next_sun_check_at(self) -> float | None:
        return self._next_sun_check_at

    async def on_location_update(self, user_id: str) -> None:
        if self._cache_path is None:
            return
        trimmed = user_id.strip()
        if trimmed == "":
            return
        async with self._process_lock:
            await self._process_location_update(trimmed)

    def request_shutdown(self) -> None:
        self._stop.set()

    def schedule_location_update(self, user_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self.on_location_update(user_id),
            name=f"rule-eval-location-{user_id}",
        )
        task.add_done_callback(_log_location_evaluation_task)

    def start_periodic_tick(self) -> None:
        if self._tick_task is not None:
            return
        self._tick_task = asyncio.create_task(
            self._periodic_loop(),
            name="rule-evaluator-tick",
        )

    async def _build_evaluation_context(
        self,
        *,
        now: datetime,
    ) -> RuleEvaluationContext:
        settings = load_settings_location()
        tz = ZoneInfo(settings.timezone)
        effective_now = now.astimezone(tz) if now.tzinfo is not None else now.replace(tzinfo=tz)
        sun = compute_rules_sun_out(settings, now=effective_now)
        cache_path = self._cache_path
        geofences = []
        user_display_names: dict[str, str] = {}
        user_locations: dict[str, UserLocationOut] = {}
        if cache_path is not None:
            geofences = [
                _geofence_record_to_out(row) for row in list_geofences(cache_path)
            ]
            users = list_users(cache_path)
            user_display_names = {
                row.user_id: row.display_name for row in users
            }
            stored = list_user_locations(cache_path)
            for uid, location in stored.items():
                user_locations[uid] = UserLocationOut(
                    accuracy_m=location.accuracy_m,
                    lat=location.lat,
                    lon=location.lon,
                    received_at=_location_received_at_iso(location),
                    source=location.source,
                )
        roster_user_id_lookup = build_roster_user_id_lookup(list(user_display_names))
        return RuleEvaluationContext(
            geofences=tuple(geofences),
            now=effective_now,
            roster_user_id_lookup=roster_user_id_lookup,
            sun=sun,
            timezone=tz,
            user_display_names=user_display_names,
            user_locations=user_locations,
        )

    async def _attempt_deferred_accuracy_edge_fires(
        self,
        *,
        user_id: str,
        location: UserLocationRecord,
        inside_ids: set[str],
        ctx: RuleEvaluationContext,
        now: float,
    ) -> None:
        keys_for_user = [
            key for key in self._deferred_accuracy_edges if key[1] == user_id
        ]
        for key in keys_for_user:
            deferred = self._deferred_accuracy_edges.get(key)
            if deferred is None or now > deferred.expires_at:
                continue
            rule = _automation_rule_by_id(deferred.rule_id)
            if rule is None or not rule.enabled or rule.trigger != "edge_true":
                self._deferred_accuracy_edges.pop(key, None)
                continue
            if not _accuracy_passes(rule, location):
                continue
            if not _deferred_state_matches(
                deferred.event,
                deferred.geofence_id,
                inside_ids,
            ):
                self._deferred_accuracy_edges.pop(key, None)
                _log_deferred_edge_cancelled(
                    rule_id=deferred.rule_id,
                    user_id=user_id,
                    geofence_id=deferred.geofence_id,
                    event=deferred.event,
                )
                continue
            evaluation = evaluate_rule(rule, ctx)
            if not evaluation.all_met:
                _log_rule_skipped(
                    rule.id,
                    user_id,
                    reason="conditions_not_met",
                    detail=_format_unmet_conditions_for_log(evaluation),
                )
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if not self._cooldown_elapsed(rule, runtime):
                remaining_s = rule.cooldown_s - (
                    now - (runtime.last_fired_at or 0.0)
                )
                _log_rule_skipped(
                    rule.id,
                    user_id,
                    reason="cooldown",
                    detail=f"remaining_s={max(0.0, remaining_s):.0f}",
                )
                continue
            self._deferred_accuracy_edges.pop(key, None)
            transitions = {
                deferred.geofence_id: GeofenceTransition(
                    entered=deferred.event == "entered",
                    left=deferred.event == "left",
                ),
            }
            await self._execute_rule(
                rule,
                evaluation=evaluation,
                transitions=transitions,
                user_id=user_id,
                fire_source="deferred",
            )

    def _clear_deferred_accuracy_edges_for_rule(
        self,
        rule_id: str,
        user_id: str,
    ) -> None:
        keys = [
            key
            for key in self._deferred_accuracy_edges
            if key[0] == rule_id and key[1] == user_id
        ]
        for key in keys:
            self._deferred_accuracy_edges.pop(key, None)

    def _cooldown_elapsed(self, rule: RuleOut, state: _RuleRuntimeState) -> bool:
        if state.last_fired_at is None:
            return True
        return self._now_fn() - state.last_fired_at >= rule.cooldown_s

    async def _execute_rule(
        self,
        rule: RuleOut,
        *,
        evaluation: RuleEvaluationResult,
        fire_source: RuleFireSource = "immediate",
        transitions: dict[str, GeofenceTransition],
        user_id: str,
    ) -> None:
        runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
        started = time.monotonic()
        errors: list[str] = []
        performed_side_effect = not rule.device_actions and not rule.notify_on_fire
        device_state = self._device_state_getter()
        if device_state is None:
            if rule.device_actions:
                errors.append("Device discovery still in progress; actions skipped")
        elif rule.device_actions:
            performed_side_effect = True
            errors.extend(
                await dispatch_rule_device_actions(device_state, rule.device_actions)
            )
        if rule.notify_on_fire and self._cache_path is not None:
            try:
                await asyncio.to_thread(
                    send_rule_notification_email,
                    self._cache_path,
                    rule=rule,
                )
                performed_side_effect = True
            except RuleActionDispatchError as exc:
                errors.append(str(exc))
        if not performed_side_effect:
            runtime.last_error = "; ".join(errors) if errors else None
            if runtime.last_error is not None:
                _LOGGER.warning(
                    "[rules] rule_id=%s edge matched but no side effect completed: %s",
                    rule.id,
                    runtime.last_error,
                )
            self._persist_rule_state(rule.id)
            return
        runtime.last_fired_at = self._now_fn()
        runtime.last_error = "; ".join(errors) if errors else None
        self._persist_rule_state(rule.id)
        self._clear_deferred_accuracy_edges_for_rule(rule.id, user_id)
        duration_ms = (time.monotonic() - started) * 1000.0
        _LOGGER.info(
            "[rules] fired rule_id=%s user_id=%s source=%s transitions=%s conditions=%s "
            "actions=%d duration_ms=%.0f%s",
            rule.id,
            user_id,
            fire_source,
            _format_geofence_transitions_for_log(transitions),
            _format_rule_conditions_for_log(rule, evaluation),
            len(rule.device_actions),
            duration_ms,
            f" errors={runtime.last_error!r}" if runtime.last_error else "",
        )

    def _expire_deferred_accuracy_edges(
        self,
        user_id: str,
        now: float,
    ) -> set[tuple[str, str, str, str]]:
        keys_to_expire = [
            key
            for key, deferred in self._deferred_accuracy_edges.items()
            if key[1] == user_id and now > deferred.expires_at
        ]
        expired_keys: set[tuple[str, str, str, str]] = set()
        for key in keys_to_expire:
            deferred = self._deferred_accuracy_edges.pop(key)
            expired_keys.add(key)
            _log_deferred_edge_expired(
                rule_id=deferred.rule_id,
                user_id=deferred.user_id,
                geofence_id=deferred.geofence_id,
                event=deferred.event,
            )
        return expired_keys

    def _load_persisted_rule_state(self) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        for record in list_rule_fire_states(cache_path).values():
            self._rule_state[record.rule_id] = _RuleRuntimeState(
                last_error=record.last_error,
                last_fired_at=record.last_fired_at,
            )

    async def _periodic_loop(self) -> None:
        while not self._stop.is_set():
            self._last_run_at = self._now_fn()
            self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RULE_EVALUATOR_TICK_S)
            except TimeoutError:
                continue

    def _persist_rule_state(self, rule_id: str) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        state = self._rule_state.get(rule_id)
        if state is None:
            return
        upsert_rule_fire_state(
            cache_path,
            last_error=state.last_error,
            last_fired_at=state.last_fired_at,
            rule_id=rule_id,
        )

    def _register_deferred_accuracy_edges(
        self,
        *,
        expired_keys: set[tuple[str, str, str, str]],
        rule: RuleOut,
        user_id: str,
        intents: list[tuple[str, DeferredGeofenceEvent]],
        grace_s: int,
        now: float,
        location: UserLocationRecord,
    ) -> None:
        for geofence_id, event in intents:
            key = (rule.id, user_id, geofence_id, event)
            if key in expired_keys:
                continue
            is_new = key not in self._deferred_accuracy_edges
            if not is_new:
                continue
            self._deferred_accuracy_edges[key] = _DeferredAccuracyEdge(
                event=event,
                expires_at=now + grace_s,
                geofence_id=geofence_id,
                observed_at=location.received_at,
                rule_id=rule.id,
                user_id=user_id,
            )
            if is_new:
                _log_deferred_edge_registered(
                    rule_id=rule.id,
                    user_id=user_id,
                    geofence_id=geofence_id,
                    event=event,
                    grace_s=grace_s,
                    accuracy_m=location.accuracy_m,
                    accuracy_limit_m=rule.min_location_accuracy_m,
                )

    async def _process_location_update(self, user_id: str) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        locations = list_user_locations(cache_path)
        location = locations.get(user_id)
        if location is None:
            return
        geofences = list_geofences(cache_path)
        transitions = self._update_geofence_transitions(
            user_id,
            location,
            geofences,
        )
        inside_ids = set(geofence_ids_containing_location(location, geofences))
        now = self._now_fn()
        expired_deferred_keys = self._expire_deferred_accuracy_edges(user_id, now)
        if transitions:
            transitions_summary = _format_geofence_transitions_for_log(transitions)
            _LOGGER.debug(
                "[rules] evaluating location update user_id=%s lat=%.5f lon=%.5f "
                "accuracy_m=%s transitions=%s",
                user_id,
                location.lat,
                location.lon,
                location.accuracy_m,
                transitions_summary,
            )
        ctx = await self._build_evaluation_context(now=datetime.now(UTC))
        await self._attempt_deferred_accuracy_edge_fires(
            user_id=user_id,
            location=location,
            inside_ids=inside_ids,
            ctx=ctx,
            now=now,
        )
        for rule in list_automation_rules():
            if not rule.enabled or rule.trigger != "edge_true":
                continue
            if user_id not in collect_rule_user_ids(rule):
                continue
            grace_s = _accuracy_edge_grace_s(rule)
            if not _accuracy_passes(rule, location):
                if grace_s is not None:
                    intents: list[tuple[str, DeferredGeofenceEvent]] = []
                    if transitions and _user_triggered_geofence_edge(
                        rule.conditions.all,
                        user_id,
                        transitions,
                    ):
                        intents = _collect_geofence_edge_intents(
                            rule.conditions.all,
                            user_id,
                            transitions,
                        )
                    if not intents:
                        intents = _collect_geofence_state_intents(
                            rule.conditions.all,
                            inside_ids,
                            user_id,
                        )
                    intents = _dedupe_geofence_intents(intents)
                    if intents:
                        self._register_deferred_accuracy_edges(
                            expired_keys=expired_deferred_keys,
                            rule=rule,
                            user_id=user_id,
                            intents=intents,
                            grace_s=grace_s,
                            now=now,
                            location=location,
                        )
                if _user_triggered_geofence_edge(
                    rule.conditions.all,
                    user_id,
                    transitions,
                ):
                    _log_rule_skipped(
                        rule.id,
                        user_id,
                        reason="location_accuracy",
                        detail=(
                            f"accuracy_m={location.accuracy_m} "
                            f"limit={rule.min_location_accuracy_m}"
                        ),
                    )
                continue
            if not transitions:
                continue
            if not _user_triggered_geofence_edge(
                rule.conditions.all,
                user_id,
                transitions,
            ):
                continue
            evaluation = evaluate_rule(rule, ctx)
            if not evaluation.all_met:
                _log_rule_skipped(
                    rule.id,
                    user_id,
                    reason="conditions_not_met",
                    detail=_format_unmet_conditions_for_log(evaluation),
                )
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if not self._cooldown_elapsed(rule, runtime):
                remaining_s = rule.cooldown_s - (now - (runtime.last_fired_at or 0.0))
                _log_rule_skipped(
                    rule.id,
                    user_id,
                    reason="cooldown",
                    detail=f"remaining_s={max(0.0, remaining_s):.0f}",
                )
                continue
            await self._execute_rule(
                rule,
                evaluation=evaluation,
                transitions=transitions,
                user_id=user_id,
            )
        self._last_run_at = now
        self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S

    def _seed_geofence_state(self) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        geofences = list_geofences(cache_path)
        locations = list_user_locations(cache_path)
        for user_id, location in locations.items():
            inside_ids = set(geofence_ids_containing_location(location, geofences))
            for geofence in geofences:
                if not geofence.enabled:
                    continue
                self._geofence_was_inside[(user_id, geofence.geofence_id)] = (
                    geofence.geofence_id in inside_ids
                )

    def _update_geofence_transitions(
        self,
        user_id: str,
        location: UserLocationRecord,
        geofences: list[GeofenceRecord],
    ) -> dict[str, GeofenceTransition]:
        inside_ids = set(geofence_ids_containing_location(location, geofences))
        transitions: dict[str, GeofenceTransition] = {}
        observed_at = location.received_at
        for geofence in geofences:
            if not geofence.enabled:
                continue
            geofence_id = geofence.geofence_id
            key = (user_id, geofence_id)
            was_inside = self._geofence_was_inside.get(key, False)
            now_inside = geofence_id in inside_ids
            transition = GeofenceTransition()
            if now_inside and not was_inside:
                outside_since = self._geofence_outside_since.get(key)
                dwell_elapsed = outside_since is None or (
                    observed_at - outside_since >= _MIN_GEOFENCE_OUTSIDE_DWELL_S
                )
                self._geofence_was_inside[key] = True
                self._geofence_outside_since.pop(key, None)
                if dwell_elapsed:
                    transition = GeofenceTransition(entered=True)
                elif outside_since is not None:
                    _log_geofence_enter_debounced(
                        user_id=user_id,
                        geofence_id=geofence_id,
                        outside_s=observed_at - outside_since,
                        dwell_remaining_s=_MIN_GEOFENCE_OUTSIDE_DWELL_S
                        - (observed_at - outside_since),
                    )
            elif was_inside and not now_inside:
                self._geofence_was_inside[key] = False
                self._geofence_outside_since[key] = observed_at
                transition = GeofenceTransition(left=True)
            elif now_inside:
                self._geofence_was_inside[key] = True
                self._geofence_outside_since.pop(key, None)
            else:
                self._geofence_was_inside[key] = False
                self._geofence_outside_since.setdefault(key, observed_at)
            if transition.entered or transition.left:
                transitions[geofence_id] = transition
                _log_geofence_transition(
                    user_id=user_id,
                    geofence_id=geofence_id,
                    transition=transition,
                )
        return transitions


def _accuracy_passes(rule: RuleOut, location: UserLocationRecord) -> bool:
    if location.accuracy_m is None:
        return True
    return location.accuracy_m <= rule.min_location_accuracy_m


def _accuracy_edge_grace_s(rule: RuleOut) -> int | None:
    grace = rule.accuracy_edge_grace_s
    if grace is None or grace <= 0:
        return None
    return grace


def _automation_rule_by_id(rule_id: str) -> RuleOut | None:
    for rule in list_automation_rules():
        if rule.id == rule_id:
            return rule
    return None


def _collect_geofence_edge_intents(
    conditions: list[RuleConditionOut],
    user_id: str,
    transitions: dict[str, GeofenceTransition],
) -> list[tuple[str, DeferredGeofenceEvent]]:
    intents: list[tuple[str, DeferredGeofenceEvent]] = []
    for condition in conditions:
        intents.extend(
            _condition_geofence_edge_intents(condition, user_id, transitions),
        )
    return intents


def _collect_geofence_state_intents(
    conditions: list[RuleConditionOut],
    inside_ids: set[str],
    user_id: str,
) -> list[tuple[str, DeferredGeofenceEvent]]:
    intents: list[tuple[str, DeferredGeofenceEvent]] = []
    for condition in conditions:
        intents.extend(
            _condition_geofence_state_intents(condition, inside_ids, user_id),
        )
    return intents


def _condition_geofence_edge_intents(
    condition: RuleConditionOut,
    user_id: str,
    transitions: dict[str, GeofenceTransition],
) -> list[tuple[str, DeferredGeofenceEvent]]:
    if isinstance(condition, UsersInsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return []
        transition = transitions.get(condition.geofence_id)
        if transition is not None and transition.entered:
            return [(condition.geofence_id, "entered")]
        return []
    if isinstance(condition, UsersOutsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return []
        transition = transitions.get(condition.geofence_id)
        if transition is not None and transition.left:
            return [(condition.geofence_id, "left")]
        return []
    if isinstance(condition, AllConditionsCondition):
        intents: list[tuple[str, DeferredGeofenceEvent]] = []
        for child in condition.conditions:
            intents.extend(
                _condition_geofence_edge_intents(child, user_id, transitions),
            )
        return intents
    if isinstance(condition, AnyConditionsCondition):
        intents = []
        for child in condition.conditions:
            intents.extend(
                _condition_geofence_edge_intents(child, user_id, transitions),
            )
        return intents
    return []


def _condition_geofence_state_intents(
    condition: RuleConditionOut,
    inside_ids: set[str],
    user_id: str,
) -> list[tuple[str, DeferredGeofenceEvent]]:
    if isinstance(condition, UsersInsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return []
        if condition.geofence_id in inside_ids:
            return [(condition.geofence_id, "entered")]
        return []
    if isinstance(condition, UsersOutsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return []
        if condition.geofence_id not in inside_ids:
            return [(condition.geofence_id, "left")]
        return []
    if isinstance(condition, AllConditionsCondition):
        intents: list[tuple[str, DeferredGeofenceEvent]] = []
        for child in condition.conditions:
            intents.extend(
                _condition_geofence_state_intents(child, inside_ids, user_id),
            )
        return intents
    if isinstance(condition, AnyConditionsCondition):
        intents = []
        for child in condition.conditions:
            intents.extend(
                _condition_geofence_state_intents(child, inside_ids, user_id),
            )
        return intents
    return []


def _condition_triggered_geofence_edge(
    condition: RuleConditionOut,
    user_id: str,
    transitions: dict[str, GeofenceTransition],
) -> bool:
    if isinstance(condition, UsersInsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return False
        transition = transitions.get(condition.geofence_id)
        return transition is not None and transition.entered
    if isinstance(condition, UsersOutsideGeofenceCondition):
        if not rule_references_user_id(condition.user_ids, user_id):
            return False
        transition = transitions.get(condition.geofence_id)
        return transition is not None and transition.left
    if isinstance(condition, AllConditionsCondition):
        if not condition.conditions:
            return False
        return any(
            _condition_triggered_geofence_edge(child, user_id, transitions)
            for child in condition.conditions
        )
    if isinstance(condition, AnyConditionsCondition):
        if not condition.conditions:
            return False
        return any(
            _condition_triggered_geofence_edge(child, user_id, transitions)
            for child in condition.conditions
        )
    return False


def _dedupe_geofence_intents(
    intents: list[tuple[str, DeferredGeofenceEvent]],
) -> list[tuple[str, DeferredGeofenceEvent]]:
    seen: set[tuple[str, DeferredGeofenceEvent]] = set()
    ordered: list[tuple[str, DeferredGeofenceEvent]] = []
    for intent in intents:
        if intent in seen:
            continue
        seen.add(intent)
        ordered.append(intent)
    return ordered


def _deferred_state_matches(
    event: DeferredGeofenceEvent,
    geofence_id: str,
    inside_ids: set[str],
) -> bool:
    if event == "entered":
        return geofence_id in inside_ids
    return geofence_id not in inside_ids


def _format_geofence_transitions_for_log(
    transitions: dict[str, GeofenceTransition],
) -> str:
    parts: list[str] = []
    for geofence_id in sorted(transitions):
        transition = transitions[geofence_id]
        if transition.entered:
            parts.append(f"{geofence_id}:entered")
        elif transition.left:
            parts.append(f"{geofence_id}:left")
    return ",".join(parts) if parts else "none"


def _format_rule_conditions_for_log(
    rule: RuleOut,
    evaluation: RuleEvaluationResult,
) -> str:
    parts: list[str] = []
    for row in evaluation.conditions:
        if rule.trigger == "edge_true" and isinstance(
            row.condition,
            (UsersInsideGeofenceCondition, UsersOutsideGeofenceCondition),
        ):
            parts.append(f"{row.label}: {row.detail}")
            continue
        state = "met" if row.met else "unmet"
        parts.append(f"{row.label}={state}")
    return ",".join(parts) if parts else "none"


def _format_unmet_conditions_for_log(evaluation: RuleEvaluationResult) -> str:
    unmet = [
        f"{row.label} ({row.detail})"
        for row in evaluation.conditions
        if not row.met
    ]
    return "; ".join(unmet) if unmet else "none"


def _log_deferred_edge_cancelled(
    *,
    rule_id: str,
    user_id: str,
    geofence_id: str,
    event: DeferredGeofenceEvent,
) -> None:
    _LOGGER.info(
        "[rules] deferred edge cancelled rule_id=%s user_id=%s geofence_id=%s event=%s",
        rule_id,
        user_id,
        geofence_id,
        event,
    )


def _log_deferred_edge_expired(
    *,
    rule_id: str,
    user_id: str,
    geofence_id: str,
    event: DeferredGeofenceEvent,
) -> None:
    _LOGGER.info(
        "[rules] deferred edge expired rule_id=%s user_id=%s geofence_id=%s event=%s",
        rule_id,
        user_id,
        geofence_id,
        event,
    )


def _log_deferred_edge_registered(
    *,
    rule_id: str,
    user_id: str,
    geofence_id: str,
    event: DeferredGeofenceEvent,
    grace_s: int,
    accuracy_m: int | None,
    accuracy_limit_m: int,
) -> None:
    _LOGGER.info(
        "[rules] deferred edge registered rule_id=%s user_id=%s geofence_id=%s "
        "event=%s grace_s=%d accuracy_m=%s accuracy_limit_m=%d",
        rule_id,
        user_id,
        geofence_id,
        event,
        grace_s,
        accuracy_m,
        accuracy_limit_m,
    )


def _log_geofence_enter_debounced(
    *,
    user_id: str,
    geofence_id: str,
    outside_s: float,
    dwell_remaining_s: float,
) -> None:
    _LOGGER.info(
        "[rules] geofence enter suppressed user_id=%s geofence_id=%s "
        "outside_s=%.0f dwell_remaining_s=%.0f",
        user_id,
        geofence_id,
        outside_s,
        max(0.0, dwell_remaining_s),
    )


def _log_geofence_transition(
    *,
    user_id: str,
    geofence_id: str,
    transition: GeofenceTransition,
) -> None:
    if transition.entered:
        event = "entered"
    elif transition.left:
        event = "left"
    else:
        return
    _LOGGER.debug(
        "[rules] geofence transition user_id=%s geofence_id=%s event=%s",
        user_id,
        geofence_id,
        event,
    )


def _log_rule_skipped(
    rule_id: str,
    user_id: str,
    *,
    reason: str,
    detail: str,
) -> None:
    _LOGGER.info(
        "[rules] skipped rule_id=%s user_id=%s reason=%s detail=%s",
        rule_id,
        user_id,
        reason,
        detail,
    )


def _geofence_record_to_out(record: GeofenceRecord):
    from app.api.schemas import GeofenceOut

    return GeofenceOut(
        center_lat=record.center_lat,
        center_lon=record.center_lon,
        enabled=record.enabled,
        geofence_id=record.geofence_id,
        label=record.label,
        owntracks_rid=record.owntracks_rid,
        radius_m=record.radius_m,
    )


def _location_received_at_iso(location: UserLocationRecord) -> str:
    return datetime.fromtimestamp(location.received_at, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _log_location_evaluation_task(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.exception(
            "[rules] location evaluation task failed",
            exc_info=exc,
        )


def _user_triggered_geofence_edge(
    conditions: list[RuleConditionOut],
    user_id: str,
    transitions: dict[str, GeofenceTransition],
) -> bool:
    if not conditions:
        return False
    return any(
        _condition_triggered_geofence_edge(condition, user_id, transitions)
        for condition in conditions
    )
