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
    compute_rules_sun_out,
    evaluate_rule_conditions_met,
)
from app.rules_store import GeofenceRecord, list_geofences, list_users

_LOGGER = logging.getLogger(__name__)
_RULE_EVALUATOR_TICK_S = 60.0


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
        self._geofence_was_inside: dict[tuple[str, str], bool] = {}
        self._last_run_at: float | None = None
        self._next_sun_check_at: float | None = None
        self._process_lock = asyncio.Lock()
        self._rule_state: dict[str, _RuleRuntimeState] = {}
        self._stop = asyncio.Event()
        self._tick_task: asyncio.Task[None] | None = None
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
        return RuleEvaluationContext(
            geofences=tuple(geofences),
            now=effective_now,
            sun=sun,
            timezone=tz,
            user_display_names=user_display_names,
            user_locations=user_locations,
        )

    def _cooldown_elapsed(self, rule: RuleOut, state: _RuleRuntimeState) -> bool:
        if state.last_fired_at is None:
            return True
        return self._now_fn() - state.last_fired_at >= rule.cooldown_s

    async def _execute_rule(self, rule: RuleOut) -> None:
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
            return
        runtime.last_fired_at = self._now_fn()
        runtime.last_error = "; ".join(errors) if errors else None
        duration_ms = (time.monotonic() - started) * 1000.0
        _LOGGER.info(
            "[rules] fired rule_id=%s actions=%d duration_ms=%.0f%s",
            rule.id,
            len(rule.device_actions),
            duration_ms,
            f" errors={runtime.last_error!r}" if runtime.last_error else "",
        )

    async def _periodic_loop(self) -> None:
        while not self._stop.is_set():
            self._last_run_at = self._now_fn()
            self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_RULE_EVALUATOR_TICK_S)
            except TimeoutError:
                continue

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
        if not transitions:
            self._last_run_at = self._now_fn()
            return
        ctx = await self._build_evaluation_context(now=datetime.now(UTC))
        for rule in list_automation_rules():
            if not rule.enabled or rule.trigger != "edge_true":
                continue
            if not _accuracy_passes(rule, location):
                continue
            if not _user_triggered_geofence_edge(
                rule.conditions.all,
                user_id,
                transitions,
            ):
                continue
            if not evaluate_rule_conditions_met(rule, ctx):
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if not self._cooldown_elapsed(rule, runtime):
                continue
            await self._execute_rule(rule)
        self._last_run_at = self._now_fn()
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
        for geofence in geofences:
            if not geofence.enabled:
                continue
            geofence_id = geofence.geofence_id
            key = (user_id, geofence_id)
            was_inside = self._geofence_was_inside.get(key, False)
            now_inside = geofence_id in inside_ids
            self._geofence_was_inside[key] = now_inside
            transition = GeofenceTransition()
            if now_inside and not was_inside:
                transition = GeofenceTransition(entered=True)
            elif was_inside and not now_inside:
                transition = GeofenceTransition(left=True)
            if transition.entered or transition.left:
                transitions[geofence_id] = transition
        return transitions


def _accuracy_passes(rule: RuleOut, location: UserLocationRecord) -> bool:
    if location.accuracy_m is None:
        return True
    return location.accuracy_m <= rule.min_location_accuracy_m


def _condition_triggered_geofence_edge(
    condition: RuleConditionOut,
    user_id: str,
    transitions: dict[str, GeofenceTransition],
) -> bool:
    if isinstance(condition, UsersInsideGeofenceCondition):
        if user_id not in condition.user_ids:
            return False
        transition = transitions.get(condition.geofence_id)
        return transition is not None and transition.entered
    if isinstance(condition, UsersOutsideGeofenceCondition):
        if user_id not in condition.user_ids:
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
