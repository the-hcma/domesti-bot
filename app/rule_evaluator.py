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
    DevicesAnyOpenCondition,
    GeofenceOut,
    RuleConditionOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceCondition,
    UsersOutsideGeofenceForSCondition,
    normalized_rule_notification_emails,
)
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.cron_schedule import (
    fired_on_same_local_calendar_day,
    local_calendar_date,
    next_scheduled_evaluate_at,
)
from app.domesti_bot_cli import DeviceManagersState
from app.geofence_transition_state_store import (
    GeofenceTransitionStateRecord,
    list_geofence_transition_states,
    upsert_geofence_transition_state,
)
from app.location_history_retention import LocationHistoryRetention
from app.mytracks_store import load_location_history_retention
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_location_history_for_user,
    list_user_locations,
)
from app.rule_actions import (
    RuleActionDispatchError,
    RuleNotificationEmailOutcome,
    dispatch_rule_device_actions,
    send_rule_notification_email,
)
from app.rule_conditions import (
    RuleEvaluationContext,
    RuleEvaluationResult,
    _evaluate_condition,
    compute_rules_sun_out,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_fire_state_store import list_rule_fire_states, upsert_rule_fire_state
from app.rule_validation import (
    build_roster_user_id_lookup,
    collect_rule_user_ids,
    rule_references_user_id,
)
from app.rules_store import GeofenceRecord, list_geofences, list_users
from app.presence_connection_type import connection_type_is_wifi
from app.wifi_home_presence import (
    history_row_geofence_inside,
    wifi_home_geofence_ids,
    wifi_home_presence_applies,
)

_LOGGER = logging.getLogger(__name__)
_GEOFENCE_SEED_MAX_HISTORY_LOOKBACK_S = 86_400.0 * 7
_GEO_INSIDE_STATE_RECONCILE_S = 600.0
_GEO_OUTSIDE_STATE_RECONCILE_S = 600.0
_MIN_GEOFENCE_OUTSIDE_DWELL_S = 300.0
_RULE_EVALUATOR_TICK_S = 60.0
DeferredGeofenceEvent = Literal["entered", "left"]
RuleFireSource = Literal["deferred", "immediate", "scheduled"]


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
    next_evaluate_at: float | None = None


@dataclass
class _RuleRuntimeState:
    last_error: str | None = None
    last_fired_at: float | None = None
    next_evaluate_at: float | None = None


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
        # Geofence streak timestamps persist in SQLite and reload on startup;
        # live ingest reconciles against history when away streaks were truncated.
        self._deferred_accuracy_edges: dict[
            tuple[str, str, str, str],
            _DeferredAccuracyEdge,
        ] = {}
        self._geofence_geo_inside_streak_since: dict[tuple[str, str], float] = {}
        self._geofence_geo_outside_streak_since: dict[tuple[str, str], float] = {}
        self._geofence_inside_since: dict[tuple[str, str], float] = {}
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
        self._seed_scheduled_evaluate_times()

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
            next_evaluate_at=state.next_evaluate_at,
        )

    def geofence_inside_since_snapshot(self) -> dict[tuple[str, str], float]:
        """Return a copy of inside-dwell streak start times keyed by user and geofence."""
        return dict(self._geofence_inside_since)

    def geofence_outside_since_snapshot(self) -> dict[tuple[str, str], float]:
        """Return a copy of outside-dwell streak start times keyed by user and geofence."""
        return dict(self._geofence_outside_since)

    @property
    def last_run_at(self) -> float | None:
        return self._last_run_at

    def next_evaluate_at_for_rule(self, rule_id: str) -> float | None:
        state = self._rule_state.get(rule_id)
        if state is None:
            return None
        return state.next_evaluate_at

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
                    connection_type=location.connection_type,
                    lat=location.lat,
                    lon=location.lon,
                    received_at=_location_received_at_iso(location),
                    source=location.source,
                )
        roster_user_id_lookup = build_roster_user_id_lookup(list(user_display_names))
        return RuleEvaluationContext(
            device_state=self._device_state_getter(),
            geofence_inside_since=self.geofence_inside_since_snapshot(),
            geofence_outside_since=self.geofence_outside_since_snapshot(),
            geofences=tuple(geofences),
            now=effective_now,
            roster_user_id_lookup=roster_user_id_lookup,
            sun=sun,
            timezone=tz,
            user_display_names=user_display_names,
            user_locations=user_locations,
        )

    def _advance_scheduled_evaluate_time(
        self,
        rule: RuleOut,
        *,
        timezone: ZoneInfo,
    ) -> None:
        cron_expr = (rule.schedule_cron or "").strip()
        if cron_expr == "":
            return
        runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone)
        runtime.next_evaluate_at = next_scheduled_evaluate_at(
            cron_expr,
            now,
            timezone,
        )

    def _apply_persisted_geofence_state(self) -> None:
        """Load geofence transition maps from SQLite persistence."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        for record in list_geofence_transition_states(cache_path).values():
            key = (record.user_id, record.geofence_id)
            self._geofence_was_inside[key] = record.was_inside
            if record.outside_since is not None:
                self._geofence_outside_since[key] = record.outside_since
            if record.inside_since is not None:
                self._geofence_inside_since[key] = record.inside_since

    def _apply_wifi_home_presence(
        self,
        user_id: str,
        location: UserLocationRecord,
        geofences: list[GeofenceRecord],
        *,
        dwell_accuracy_limit_m: int | None,
        observed_at: float,
    ) -> None:
        """Sync home geofence state from low-accuracy WiFi without enter edges."""
        if not connection_type_is_wifi(location.connection_type):
            return
        settings = load_settings_location()
        target_ids = wifi_home_geofence_ids(settings, geofences)
        if not target_ids:
            return
        edge_accuracy_limit_m = _geofence_edge_accuracy_limit_m(list_automation_rules())
        wifi_accuracy_limit_m = edge_accuracy_limit_m
        if wifi_accuracy_limit_m is None:
            wifi_accuracy_limit_m = dwell_accuracy_limit_m
        if wifi_accuracy_limit_m is None:
            return
        for geofence in geofences:
            geofence_id = geofence.geofence_id
            if geofence_id not in target_ids:
                continue
            if not wifi_home_presence_applies(
                settings,
                geofence_id,
                location.connection_type,
                accuracy_m=location.accuracy_m,
                geofences=geofences,
                lat=location.lat,
                lon=location.lon,
                min_accuracy_m=wifi_accuracy_limit_m,
            ):
                continue
            key = (user_id, geofence_id)
            _log_wifi_home_presence_overrode_low_accuracy(
                user_id=user_id,
                geofence_id=geofence_id,
                accuracy_m=location.accuracy_m,
                threshold_m=wifi_accuracy_limit_m,
            )
            was_inside = self._geofence_was_inside.get(key, False)
            if not was_inside:
                self._geofence_was_inside[key] = True
                self._geofence_outside_since.pop(key, None)
                _log_wifi_home_presence_reconciled(
                    user_id=user_id,
                    geofence_id=geofence_id,
                    connection_type=location.connection_type,
                )
            if (
                dwell_accuracy_limit_m is not None
                and key not in self._geofence_inside_since
            ):
                self._geofence_inside_since[key] = observed_at
            self._persist_geofence_transition_state(
                user_id,
                geofence_id,
                last_location_received_at=observed_at,
            )

    def _backfill_geofence_state_for_key(
        self,
        *,
        dwell_accuracy_limit_m: int | None,
        edge_accuracy_limit_m: int | None,
        geofence: GeofenceRecord,
        history_since: float,
        location: UserLocationRecord,
        user_id: str,
    ) -> None:
        """Seed one ``(user_id, geofence_id)`` from history or latest location, then persist."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        geofence_id = geofence.geofence_id
        key = (user_id, geofence_id)
        self._geofence_was_inside.pop(key, None)
        self._geofence_outside_since.pop(key, None)
        self._geofence_inside_since.pop(key, None)
        history = list_user_location_history_for_user(
            cache_path,
            user_id,
            since=history_since,
        )
        settings = load_settings_location()
        geofence_list = list_geofences(cache_path)
        seeded = False
        if history:
            was_inside, outside_since, inside_since = (
                _reconstruct_geofence_seed_from_history(
                    geofence,
                    history,
                    dwell_accuracy_limit_m=dwell_accuracy_limit_m,
                    edge_accuracy_limit_m=edge_accuracy_limit_m,
                    geofences=geofence_list,
                    settings=settings,
                    user_id=user_id,
                )
            )
            if was_inside is not None:
                self._geofence_was_inside[key] = was_inside
                seeded = True
                if not was_inside and outside_since is not None:
                    self._geofence_outside_since[key] = outside_since
            if inside_since is not None:
                self._geofence_inside_since[key] = inside_since
                seeded = True
        if not seeded:
            edge_inside = history_row_geofence_inside(
                location,
                geofence,
                geofence_list,
                settings=settings,
                min_accuracy_m=edge_accuracy_limit_m,
            )
            dwell_inside = history_row_geofence_inside(
                location,
                geofence,
                geofence_list,
                settings=settings,
                min_accuracy_m=dwell_accuracy_limit_m,
            )
            if edge_inside is not None:
                self._geofence_was_inside[key] = edge_inside
                seeded = True
            if dwell_inside and dwell_accuracy_limit_m is not None:
                self._geofence_inside_since[key] = location.received_at
                seeded = True
                if wifi_home_presence_applies(
                    settings,
                    geofence_id,
                    location.connection_type,
                    accuracy_m=location.accuracy_m,
                    geofences=geofence_list,
                    lat=location.lat,
                    lon=location.lon,
                    min_accuracy_m=dwell_accuracy_limit_m,
                ):
                    _log_wifi_home_presence_overrode_low_accuracy(
                        user_id=user_id,
                        geofence_id=geofence_id,
                        accuracy_m=location.accuracy_m,
                        threshold_m=dwell_accuracy_limit_m,
                    )
            elif dwell_inside is False and dwell_accuracy_limit_m is not None:
                self._geofence_outside_since[key] = location.received_at
                seeded = True
        if seeded:
            self._persist_geofence_transition_state(
                user_id,
                geofence_id,
                last_location_received_at=location.received_at,
            )

    async def _evaluate_scheduled_rules(self) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        settings = load_settings_location()
        timezone = ZoneInfo(settings.timezone)
        now_epoch = self._now_fn()
        now = datetime.fromtimestamp(now_epoch, tz=timezone)
        ctx = await self._build_evaluation_context(now=now)
        for rule in list_automation_rules():
            if not rule.enabled or rule.trigger != "scheduled":
                continue
            cron_expr = (rule.schedule_cron or "").strip()
            if cron_expr == "":
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if runtime.next_evaluate_at is None:
                runtime.next_evaluate_at = next_scheduled_evaluate_at(
                    cron_expr,
                    now,
                    timezone,
                    due_if_matching=True,
                )
            if runtime.next_evaluate_at > now_epoch:
                continue
            log_user_ids = _scheduled_rule_user_ids_for_log(rule, ctx)
            evaluation = evaluate_rule(rule, ctx)
            _LOGGER.info(
                "[rules] scheduled evaluate rule_id=%s met=%s",
                rule.id,
                evaluation.all_met,
            )
            if evaluation.all_met:
                if rule.fire_once_per_local_day and fired_on_same_local_calendar_day(
                    runtime.last_fired_at,
                    now_epoch,
                    timezone,
                ):
                    fired_date = local_calendar_date(
                        runtime.last_fired_at or now_epoch,
                        timezone,
                    )
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="daily_cap",
                        detail=f"last_fired_local_date={fired_date.isoformat()}",
                    )
                elif self._cooldown_elapsed(rule, runtime):
                    await self._execute_rule(
                        rule,
                        evaluation=evaluation,
                        fire_source="scheduled",
                        log_user_ids=log_user_ids,
                        transitions={},
                    )
                else:
                    remaining_s = rule.cooldown_s - (
                        now_epoch - (runtime.last_fired_at or 0.0)
                    )
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="cooldown",
                        detail=f"remaining_s={max(0.0, remaining_s):.0f}",
                    )
            else:
                _log_rule_skipped(
                    rule.id,
                    log_user_ids,
                    reason="conditions_not_met",
                    detail=_format_unmet_conditions_for_log(evaluation),
                )
            self._advance_scheduled_evaluate_time(
                rule,
                timezone=timezone,
            )

    async def _attempt_deferred_accuracy_edge_fires(
        self,
        *,
        user_id: str,
        location: UserLocationRecord,
        inside_ids: set[str],
        ctx: RuleEvaluationContext,
        now: float,
    ) -> set[str]:
        fired_rule_ids: set[str] = set()
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
                edge_user_id=user_id,
                evaluation=evaluation,
                fire_source="deferred",
                log_user_ids=user_id,
                transitions=transitions,
            )
            fired_rule_ids.add(rule.id)
        return fired_rule_ids

    def _clear_deferred_accuracy_edges_for_geofence(
        self,
        user_id: str,
        geofence_id: str,
        *,
        event: DeferredGeofenceEvent,
    ) -> None:
        keys = [
            key
            for key in self._deferred_accuracy_edges
            if key[1] == user_id and key[2] == geofence_id and key[3] == event
        ]
        for key in keys:
            self._deferred_accuracy_edges.pop(key, None)

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
        edge_user_id: str | None = None,
        evaluation: RuleEvaluationResult,
        fire_source: RuleFireSource = "immediate",
        log_user_ids: str,
        transitions: dict[str, GeofenceTransition],
    ) -> None:
        runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
        started = time.monotonic()
        errors: list[str] = []
        probable_successes: list[str] = []
        email_outcome: RuleNotificationEmailOutcome | None = None
        email_error: str | None = None
        performed_side_effect = not rule.device_actions and not rule.notify_on_fire
        device_state = self._device_state_getter()
        if device_state is None:
            if rule.device_actions:
                errors.append("Device discovery still in progress; actions skipped")
        elif rule.device_actions:
            dispatch_result = await dispatch_rule_device_actions(
                device_state,
                rule.device_actions,
            )
            errors.extend(dispatch_result.errors)
            probable_successes = list(dispatch_result.probable_successes)
            if not dispatch_result.errors:
                performed_side_effect = True
        if rule.notify_on_fire and self._cache_path is not None:
            try:
                ctx = await self._build_evaluation_context(
                    now=datetime.fromtimestamp(self._now_fn(), tz=UTC),
                )
                email_outcome = await asyncio.to_thread(
                    send_rule_notification_email,
                    self._cache_path,
                    notification_detail=_notification_detail_from_evaluation(
                        rule,
                        ctx,
                    ),
                    rule=rule,
                )
                performed_side_effect = True
            except RuleActionDispatchError as exc:
                email_error = str(exc)
                errors.append(email_error)
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
        runtime.last_error = "; ".join(errors) if errors else None
        if errors:
            _LOGGER.warning(
                "[rules] rule_id=%s matched with partial side-effect failures: %s",
                rule.id,
                runtime.last_error,
            )
        runtime.last_fired_at = self._now_fn()
        self._persist_rule_state(rule.id)
        deferred_user_id = edge_user_id or log_user_ids.partition(",")[0]
        self._clear_deferred_accuracy_edges_for_rule(rule.id, deferred_user_id)
        duration_ms = (time.monotonic() - started) * 1000.0
        _LOGGER.info(
            "[rules] fired rule_id=%s user_ids=%s source=%s transitions=%s conditions=%s "
            "actions=%d email=%s duration_ms=%.0f%s%s",
            rule.id,
            log_user_ids,
            fire_source,
            _format_geofence_transitions_for_log(transitions),
            _format_rule_conditions_for_log(rule, evaluation),
            len(rule.device_actions),
            _format_rule_email_outcome_for_log(
                rule,
                cache_path=self._cache_path,
                email_error=email_error,
                email_outcome=email_outcome,
            ),
            duration_ms,
            f" errors={runtime.last_error!r}" if runtime.last_error else "",
            f" probable={'; '.join(probable_successes)!r}" if probable_successes else "",
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
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=_RULE_EVALUATOR_TICK_S,
                )
                break
            except TimeoutError:
                pass
            self._last_run_at = self._now_fn()
            self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S
            try:
                async with self._process_lock:
                    await self._evaluate_scheduled_rules()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("[rules] scheduled rule tick failed")

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

    def _persist_geofence_transition_state(
        self,
        user_id: str,
        geofence_id: str,
        *,
        last_location_received_at: float | None,
    ) -> None:
        """Write the in-memory geofence maps for one pair to SQLite."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        key = (user_id, geofence_id)
        upsert_geofence_transition_state(
            cache_path,
            geofence_id=geofence_id,
            inside_since=self._geofence_inside_since.get(key),
            last_location_received_at=last_location_received_at,
            outside_since=self._geofence_outside_since.get(key),
            user_id=user_id,
            was_inside=self._geofence_was_inside.get(key, False),
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

    def _reconcile_geofence_outside_since_from_history(
        self,
        user_id: str,
        location: UserLocationRecord,
        geofences: list[GeofenceRecord],
        *,
        edge_accuracy_limit_m: int | None,
        history_since: float,
    ) -> None:
        """Expand ``outside_since`` when history shows a longer away streak than memory."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        if not _location_accuracy_passes(location, edge_accuracy_limit_m):
            return
        inside_ids = set(geofence_ids_containing_location(location, geofences))
        history = list_user_location_history_for_user(
            cache_path,
            user_id,
            since=history_since,
        )
        if not history:
            return
        for geofence in geofences:
            if not geofence.enabled:
                continue
            geofence_id = geofence.geofence_id
            if geofence_id in inside_ids:
                continue
            key = (user_id, geofence_id)
            _, history_outside_since, _ = _reconstruct_geofence_seed_from_history(
                geofence,
                history,
                dwell_accuracy_limit_m=None,
                edge_accuracy_limit_m=edge_accuracy_limit_m,
                geofences=geofences,
                settings=load_settings_location(),
            )
            if history_outside_since is None:
                continue
            current = self._geofence_outside_since.get(key)
            if current is not None and history_outside_since >= current:
                continue
            self._geofence_was_inside[key] = False
            self._geofence_outside_since[key] = history_outside_since
            self._persist_geofence_transition_state(
                user_id,
                geofence_id,
                last_location_received_at=location.received_at,
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
        rules = list_automation_rules()
        edge_accuracy_limit_m = _geofence_edge_accuracy_limit_m(rules)
        dwell_accuracy_limit_m = _geofence_dwell_accuracy_limit_m(rules)
        transitions = self._update_geofence_transitions(
            user_id,
            location,
            geofences,
            accuracy_limit_m=edge_accuracy_limit_m,
            dwell_accuracy_limit_m=dwell_accuracy_limit_m,
        )
        self._apply_wifi_home_presence(
            user_id,
            location,
            geofences,
            dwell_accuracy_limit_m=dwell_accuracy_limit_m,
            observed_at=location.received_at,
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
        ctx = await self._build_evaluation_context(
            now=datetime.fromtimestamp(self._now_fn(), tz=UTC),
        )
        deferred_fired_rule_ids = await self._attempt_deferred_accuracy_edge_fires(
            user_id=user_id,
            location=location,
            inside_ids=inside_ids,
            ctx=ctx,
            now=now,
        )
        for rule in rules:
            if not rule.enabled or rule.trigger != "edge_true":
                continue
            if rule.id in deferred_fired_rule_ids:
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
                edge_user_id=user_id,
                evaluation=evaluation,
                log_user_ids=user_id,
                transitions=transitions,
            )
        self._last_run_at = now
        self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S

    def _seed_geofence_state(self) -> None:
        """Initialize geofence transition maps from SQLite with one-time history backfill."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        geofences = list_geofences(cache_path)
        locations = list_user_locations(cache_path)
        rules = list_automation_rules()
        edge_accuracy_limit_m = _geofence_edge_accuracy_limit_m(rules)
        dwell_accuracy_limit_m = _geofence_dwell_accuracy_limit_m(rules)
        self._apply_persisted_geofence_state()
        persisted = list_geofence_transition_states(cache_path)
        retention = load_location_history_retention(cache_path)
        history_since = _geofence_seed_history_since_epoch(
            rules,
            now=self._now_fn(),
            retention=retention,
        )
        for user_id, location in locations.items():
            for geofence in geofences:
                if not geofence.enabled:
                    continue
                key = (user_id, geofence.geofence_id)
                record = persisted.get(key)
                if record is not None and _persisted_geofence_state_covers_location(
                    record,
                    location,
                    geofence,
                ):
                    continue
                self._backfill_geofence_state_for_key(
                    dwell_accuracy_limit_m=dwell_accuracy_limit_m,
                    edge_accuracy_limit_m=edge_accuracy_limit_m,
                    geofence=geofence,
                    history_since=history_since,
                    location=location,
                    user_id=user_id,
                )

    def _seed_scheduled_evaluate_times(self) -> None:
        settings = load_settings_location()
        timezone = ZoneInfo(settings.timezone)
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone)
        for rule in list_automation_rules():
            if not rule.enabled or rule.trigger != "scheduled":
                continue
            cron_expr = (rule.schedule_cron or "").strip()
            if cron_expr == "":
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if runtime.next_evaluate_at is None:
                runtime.next_evaluate_at = next_scheduled_evaluate_at(
                    cron_expr,
                    now,
                    timezone,
                    due_if_matching=True,
                )

    def _update_geofence_transitions(
        self,
        user_id: str,
        location: UserLocationRecord,
        geofences: list[GeofenceRecord],
        *,
        accuracy_limit_m: int | None,
        dwell_accuracy_limit_m: int | None,
    ) -> dict[str, GeofenceTransition]:
        inside_ids = set(geofence_ids_containing_location(location, geofences))
        mutate_state = _location_accuracy_passes(location, accuracy_limit_m)
        was_inside_before_history_reconcile = {
            key: value
            for key, value in self._geofence_was_inside.items()
            if key[0] == user_id
        }
        if not mutate_state and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "[rules] geofence edge state unchanged user_id=%s "
                "accuracy_m=%s limit_m=%s",
                user_id,
                location.accuracy_m,
                accuracy_limit_m,
            )
        if mutate_state and self._cache_path is not None:
            rules = list_automation_rules()
            retention = load_location_history_retention(self._cache_path)
            history_since = _geofence_seed_history_since_epoch(
                rules,
                now=location.received_at,
                retention=retention,
            )
            self._reconcile_geofence_outside_since_from_history(
                user_id,
                location,
                geofences,
                edge_accuracy_limit_m=accuracy_limit_m,
                history_since=history_since,
            )
        return self._compute_geofence_transitions(
            user_id,
            geofences,
            inside_ids,
            observed_at=location.received_at,
            dwell_accuracy_limit_m=dwell_accuracy_limit_m,
            location=location,
            mutate_state=mutate_state,
            was_inside_before_history_reconcile=was_inside_before_history_reconcile,
        )

    def _compute_geofence_transitions(
        self,
        user_id: str,
        geofences: list[GeofenceRecord],
        inside_ids: set[str],
        *,
        dwell_accuracy_limit_m: int | None,
        location: UserLocationRecord,
        observed_at: float,
        mutate_state: bool,
        was_inside_before_history_reconcile: dict[tuple[str, str], bool],
    ) -> dict[str, GeofenceTransition]:
        transitions: dict[str, GeofenceTransition] = {}
        settings = load_settings_location()
        for geofence in geofences:
            if not geofence.enabled:
                continue
            geofence_id = geofence.geofence_id
            key = (user_id, geofence_id)
            was_inside = self._geofence_was_inside.get(key, False)
            prior_was_inside = was_inside_before_history_reconcile.get(
                key,
                was_inside,
            )
            depart_edge_pending = prior_was_inside and not was_inside
            gps_inside = geofence_id in inside_ids
            wifi_dwell_inside = False
            if dwell_accuracy_limit_m is not None:
                wifi_dwell_inside = wifi_home_presence_applies(
                    settings,
                    geofence_id,
                    location.connection_type,
                    accuracy_m=location.accuracy_m,
                    geofences=geofences,
                    lat=location.lat,
                    lon=location.lon,
                    min_accuracy_m=dwell_accuracy_limit_m,
                )
            now_inside_for_dwell = gps_inside or wifi_dwell_inside
            track_dwell = dwell_accuracy_limit_m is not None and (
                _location_accuracy_passes(location, dwell_accuracy_limit_m)
                or wifi_dwell_inside
            )
            transition = GeofenceTransition()
            if gps_inside:
                self._geofence_geo_outside_streak_since.pop(key, None)
                streak_since = self._geofence_geo_inside_streak_since.get(key)
                if streak_since is None:
                    streak_since = observed_at
                    self._geofence_geo_inside_streak_since[key] = streak_since
                if (
                    not was_inside
                    and observed_at - streak_since >= _GEO_INSIDE_STATE_RECONCILE_S
                ):
                    outside_since = self._geofence_outside_since.get(key)
                    dwell_elapsed = outside_since is None or (
                        observed_at - outside_since >= _MIN_GEOFENCE_OUTSIDE_DWELL_S
                    )
                    if mutate_state:
                        self._geofence_was_inside[key] = True
                        self._geofence_outside_since.pop(key, None)
                        was_inside = True
                        if track_dwell:
                            self._geofence_inside_since.setdefault(key, streak_since)
                        self._clear_deferred_accuracy_edges_for_geofence(
                            user_id,
                            geofence_id,
                            event="entered",
                        )
                        _log_geofence_inside_reconciled(
                            geofence_id=geofence_id,
                            streak_s=observed_at - streak_since,
                            user_id=user_id,
                        )
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
            else:
                self._geofence_geo_inside_streak_since.pop(key, None)
                outside_streak_since = self._geofence_geo_outside_streak_since.get(key)
                if outside_streak_since is None:
                    outside_streak_since = observed_at
                    self._geofence_geo_outside_streak_since[key] = outside_streak_since
                if (
                    (was_inside or prior_was_inside)
                    and observed_at - outside_streak_since
                    >= _GEO_OUTSIDE_STATE_RECONCILE_S
                ):
                    if mutate_state:
                        self._geofence_was_inside[key] = False
                        was_inside = False
                        if track_dwell:
                            self._geofence_inside_since.pop(key, None)
                        self._geofence_outside_since.setdefault(key, outside_streak_since)
                        self._clear_deferred_accuracy_edges_for_geofence(
                            user_id,
                            geofence_id,
                            event="left",
                        )
                        _log_geofence_outside_reconciled(
                            geofence_id=geofence_id,
                            streak_s=observed_at - outside_streak_since,
                            user_id=user_id,
                        )
                        transition = GeofenceTransition(left=True)
            if gps_inside and not was_inside:
                outside_since = self._geofence_outside_since.get(key)
                dwell_elapsed = outside_since is None or (
                    observed_at - outside_since >= _MIN_GEOFENCE_OUTSIDE_DWELL_S
                )
                if mutate_state:
                    self._geofence_was_inside[key] = True
                    self._geofence_outside_since.pop(key, None)
                    if track_dwell:
                        self._geofence_inside_since[key] = observed_at
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
            elif (was_inside or depart_edge_pending) and not now_inside_for_dwell:
                if mutate_state:
                    self._geofence_was_inside[key] = False
                    if track_dwell:
                        self._geofence_inside_since.pop(key, None)
                    transition = GeofenceTransition(left=True)
                if track_dwell:
                    self._geofence_outside_since.setdefault(key, observed_at)
                    self._geofence_inside_since.pop(key, None)
            elif now_inside_for_dwell:
                if mutate_state or wifi_dwell_inside:
                    self._geofence_was_inside[key] = True
                    self._geofence_outside_since.pop(key, None)
                if track_dwell and key not in self._geofence_inside_since:
                    self._geofence_inside_since[key] = observed_at
                    if wifi_dwell_inside:
                        _log_wifi_home_presence_overrode_low_accuracy(
                            user_id=user_id,
                            geofence_id=geofence_id,
                            accuracy_m=location.accuracy_m,
                            threshold_m=dwell_accuracy_limit_m or 0,
                        )
            else:
                if mutate_state:
                    self._geofence_was_inside[key] = False
                if track_dwell:
                    self._geofence_outside_since.setdefault(key, observed_at)
                    self._geofence_inside_since.pop(key, None)
            if transition.entered or transition.left:
                transitions[geofence_id] = transition
                _log_geofence_transition(
                    user_id=user_id,
                    geofence_id=geofence_id,
                    transition=transition,
                )
            if mutate_state:
                self._persist_geofence_transition_state(
                    user_id,
                    geofence_id,
                    last_location_received_at=observed_at,
                )
        return transitions


def _accuracy_passes(rule: RuleOut, location: UserLocationRecord) -> bool:
    return _location_accuracy_passes(location, rule.min_location_accuracy_m)


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


def _condition_has_dwell(condition: RuleConditionOut) -> bool:
    if isinstance(
        condition,
        UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition,
    ):
        return True
    if isinstance(condition, AllConditionsCondition):
        return any(_condition_has_dwell(child) for child in condition.conditions)
    if isinstance(condition, AnyConditionsCondition):
        return any(_condition_has_dwell(child) for child in condition.conditions)
    return False


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


def _format_rule_email_outcome_for_log(
    rule: RuleOut,
    *,
    cache_path: Path | None,
    email_error: str | None = None,
    email_outcome: RuleNotificationEmailOutcome | None = None,
) -> str:
    if not rule.notify_on_fire:
        return RuleNotificationEmailOutcome.disabled().format_for_log()
    if email_outcome is not None:
        return email_outcome.format_for_log(redact_recipients=True)
    recipients = normalized_rule_notification_emails(rule)
    if not recipients:
        return "skipped reason=no_recipient"
    recipient_list = ",".join(recipients)
    if email_error:
        return f"failed to={recipient_list} detail={email_error}"
    if cache_path is None:
        return f"not_attempted to={recipient_list}"
    return f"not_attempted to={recipient_list}"


def _notification_detail_from_condition(
    condition: RuleConditionOut,
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> str | None:
    if isinstance(condition, DevicesAnyOpenCondition):
        row = _evaluate_condition(condition, rule, ctx)
        if row.met:
            return row.detail
        return None
    if isinstance(condition, AllConditionsCondition):
        for child in condition.conditions:
            detail = _notification_detail_from_condition(child, rule, ctx)
            if detail is not None:
                return detail
        return None
    if isinstance(condition, AnyConditionsCondition):
        for child in condition.conditions:
            detail = _notification_detail_from_condition(child, rule, ctx)
            if detail is not None:
                return detail
        return None
    return None


def _notification_detail_from_evaluation(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> str | None:
    for condition in rule.conditions.all:
        detail = _notification_detail_from_condition(condition, rule, ctx)
        if detail is not None:
            return detail
    return None


def _format_unmet_conditions_for_log(evaluation: RuleEvaluationResult) -> str:
    unmet = [
        f"{row.label} ({row.detail})"
        for row in evaluation.conditions
        if not row.met
    ]
    return "; ".join(unmet) if unmet else "none"


def _geofence_dwell_accuracy_limit_m(rules: list[RuleOut]) -> int | None:
    """Return the strictest accuracy limit among enabled rules with dwell conditions."""
    limits = [
        rule.min_location_accuracy_m
        for rule in rules
        if rule.enabled and _rule_has_dwell_condition(rule)
    ]
    if not limits:
        return None
    return min(limits)


def _geofence_edge_accuracy_limit_m(rules: list[RuleOut]) -> int | None:
    """Return the strictest accuracy limit among enabled ``edge_true`` rules."""
    limits = [
        rule.min_location_accuracy_m
        for rule in rules
        if rule.enabled and rule.trigger == "edge_true"
    ]
    if not limits:
        return None
    return min(limits)


def _geofence_seed_history_since_epoch(
    rules: list[RuleOut],
    *,
    now: float,
    retention: LocationHistoryRetention,
) -> float:
    """Return the oldest ``received_at`` to load when seeding geofence streak state."""
    lookback_s = _MIN_GEOFENCE_OUTSIDE_DWELL_S
    lookback_s = max(lookback_s, _max_dwell_min_inside_s(rules))
    for rule in rules:
        if not rule.enabled:
            continue
        grace_s = rule.accuracy_edge_grace_s
        if grace_s is not None and grace_s > 0:
            lookback_s = max(lookback_s, float(grace_s))
    if retention.unlimited:
        lookback_s = max(lookback_s, _GEOFENCE_SEED_MAX_HISTORY_LOOKBACK_S)
    else:
        lookback_s = max(lookback_s, retention.max_age_s)
    return now - lookback_s


def _geofence_record_to_out(record: GeofenceRecord) -> GeofenceOut:
    """Map a persisted geofence row to the HTTP response schema."""
    return GeofenceOut(
        center_lat=record.center_lat,
        center_lon=record.center_lon,
        enabled=record.enabled,
        geofence_id=record.geofence_id,
        label=record.label,
        owntracks_rid=record.owntracks_rid,
        radius_m=record.radius_m,
    )


def _location_accuracy_passes(
    location: UserLocationRecord,
    limit_m: int | None,
) -> bool:
    """Return whether ``location`` meets an optional accuracy ceiling in metres."""
    if limit_m is None:
        return True
    if location.accuracy_m is None:
        return True
    return location.accuracy_m <= limit_m


def _location_received_at_iso(location: UserLocationRecord) -> str:
    """Format ``location.received_at`` as a UTC ISO-8601 string with a ``Z`` suffix."""
    return datetime.fromtimestamp(location.received_at, tz=UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _max_dwell_min_inside_s(rules: list[RuleOut]) -> float:
    """Return the largest ``min_inside_s`` across enabled dwell rules."""
    max_s = 0.0
    for rule in rules:
        if not rule.enabled:
            continue
        max_s = max(
            max_s,
            _max_dwell_min_inside_s_from_conditions(rule.conditions.all),
        )
    return max_s


def _max_dwell_min_inside_s_from_conditions(
    conditions: list[RuleConditionOut],
) -> float:
    """Return the largest ``min_inside_s`` nested under ``conditions``."""
    max_s = 0.0
    for condition in conditions:
        if isinstance(
            condition,
            UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition,
        ):
            if isinstance(condition, UsersInsideGeofenceForSCondition):
                max_s = max(max_s, float(condition.min_inside_s))
            else:
                max_s = max(max_s, float(condition.min_outside_s))
        elif isinstance(condition, AllConditionsCondition):
            max_s = max(
                max_s,
                _max_dwell_min_inside_s_from_conditions(condition.conditions),
            )
        elif isinstance(condition, AnyConditionsCondition):
            max_s = max(
                max_s,
                _max_dwell_min_inside_s_from_conditions(condition.conditions),
            )
    return max_s


def _persisted_geofence_state_covers_location(
    record: GeofenceTransitionStateRecord,
    location: UserLocationRecord,
    geofence: GeofenceRecord,
) -> bool:
    """Return whether a persisted row already reflects the latest location reading."""
    if record.last_location_received_at is None:
        return False
    if record.last_location_received_at < location.received_at:
        return False
    inside_ids = set(geofence_ids_containing_location(location, [geofence]))
    now_inside = geofence.geofence_id in inside_ids
    return record.was_inside == now_inside


def _reconstruct_geofence_seed_from_history(
    geofence: GeofenceRecord,
    history: list[UserLocationRecord],
    *,
    dwell_accuracy_limit_m: int | None,
    edge_accuracy_limit_m: int | None,
    geofences: list[GeofenceRecord],
    settings: SettingsLocationOut,
    user_id: str | None = None,
) -> tuple[bool | None, float | None, float | None]:
    """Rebuild geofence streak timestamps from ordered location history rows."""
    if not history:
        return None, None, None
    was_inside: bool | None = None
    for row in reversed(history):
        inside = history_row_geofence_inside(
            row,
            geofence,
            geofences,
            settings=settings,
            min_accuracy_m=edge_accuracy_limit_m,
        )
        if inside is None:
            continue
        was_inside = inside
        break
    outside_since: float | None = None
    if was_inside is False:
        streak_start: float | None = None
        for row in reversed(history):
            inside = history_row_geofence_inside(
                row,
                geofence,
                geofences,
                settings=settings,
                min_accuracy_m=edge_accuracy_limit_m,
            )
            if inside is None:
                continue
            if inside:
                break
            streak_start = row.received_at
        if streak_start is not None:
            outside_since = streak_start
    inside_since: float | None = None
    if dwell_accuracy_limit_m is not None:
        currently_inside_dwell: bool | None = None
        for row in reversed(history):
            inside = history_row_geofence_inside(
                row,
                geofence,
                geofences,
                settings=settings,
                min_accuracy_m=dwell_accuracy_limit_m,
            )
            if inside is None:
                continue
            currently_inside_dwell = inside
            break
        if currently_inside_dwell:
            streak_start = None
            streak_wifi_row: UserLocationRecord | None = None
            for row in reversed(history):
                inside = history_row_geofence_inside(
                    row,
                    geofence,
                    geofences,
                    settings=settings,
                    min_accuracy_m=dwell_accuracy_limit_m,
                )
                if inside is None:
                    continue
                if not inside:
                    break
                streak_start = row.received_at
                if wifi_home_presence_applies(
                    settings,
                    geofence.geofence_id,
                    row.connection_type,
                    accuracy_m=row.accuracy_m,
                    geofences=geofences,
                    lat=row.lat,
                    lon=row.lon,
                    min_accuracy_m=dwell_accuracy_limit_m,
                ):
                    streak_wifi_row = row
            inside_since = streak_start
            if (
                user_id is not None
                and streak_wifi_row is not None
                and inside_since == streak_wifi_row.received_at
            ):
                _log_wifi_home_presence_overrode_low_accuracy(
                    user_id=user_id,
                    geofence_id=geofence.geofence_id,
                    accuracy_m=streak_wifi_row.accuracy_m,
                    threshold_m=dwell_accuracy_limit_m,
                )
    return was_inside, outside_since, inside_since


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


def _log_geofence_inside_reconciled(
    *,
    geofence_id: str,
    streak_s: float,
    user_id: str,
) -> None:
    _LOGGER.info(
        "[rules] geofence inside reconciled user_id=%s geofence_id=%s streak_s=%.0f",
        user_id,
        geofence_id,
        streak_s,
    )


def _log_geofence_outside_reconciled(
    *,
    geofence_id: str,
    streak_s: float,
    user_id: str,
) -> None:
    _LOGGER.info(
        "[rules] geofence outside reconciled user_id=%s geofence_id=%s streak_s=%.0f",
        user_id,
        geofence_id,
        streak_s,
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


def _log_location_evaluation_task(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.exception(
            "[rules] location evaluation task failed",
            exc_info=exc,
        )


def _log_wifi_home_presence_overrode_low_accuracy(
    *,
    user_id: str,
    geofence_id: str,
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


def _log_wifi_home_presence_reconciled(
    *,
    user_id: str,
    geofence_id: str,
    connection_type: str | None,
) -> None:
    _LOGGER.info(
        "[rules] wifi home presence reconciled user_id=%s geofence_id=%s connection_type=%s",
        user_id,
        geofence_id,
        connection_type,
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


def _rule_has_dwell_condition(rule: RuleOut) -> bool:
    return any(_condition_has_dwell(condition) for condition in rule.conditions.all)


def _scheduled_rule_user_ids_for_log(
    rule: RuleOut,
    ctx: RuleEvaluationContext,
) -> str:
    presence_ids = presence_user_ids_for_rule(rule, ctx)
    if presence_ids:
        return ",".join(presence_ids)
    referenced = sorted(collect_rule_user_ids(rule))
    return ",".join(referenced)


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
