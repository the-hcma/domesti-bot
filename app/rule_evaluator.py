"""Asyncio automation rule evaluator on location ingest."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AllConditionsCondition,
    AnyConditionsCondition,
    DevicesAllInStateCondition,
    DevicesAnyInStateCondition,
    DevicesAnyInStateForSCondition,
    GeofenceOut,
    RuleConditionOut,
    RuleDeviceActionOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceCondition,
    UsersOutsideGeofenceForSCondition,
    normalized_rule_notification_emails,
)
from app.astronomical_schedule import (
    astronomical_repeat_cron,
    materialize_astronomical_cron,
    next_astronomical_repeat_evaluate_at,
    parse_schedule_materialized_for,
    schedule_materialized_for_date,
    uses_astronomical_eligibility_wake,
    uses_astronomical_materialized_schedule,
    uses_astronomical_repeat_schedule,
)
from app.automation_rules_loader import (
    list_automation_rules,
    load_settings_location,
    load_vacation_mode_settings,
)
from app.cron_schedule import (
    fired_on_same_local_calendar_day,
    local_calendar_date,
    next_scheduled_evaluate_at,
)
from app.deferred_device_action_store import (
    delete_deferred_device_actions,
    delete_deferred_device_actions_for_rule,
    insert_deferred_device_action,
    list_deferred_device_actions,
)
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    RuleEvaluationCause,
    RuleTrigger,
)
from app.domesti_bot_cli import DeviceManagersState
from app.dwell_watch_index import (
    DeviceDwellWatch,
    DwellDirection,
    build_device_dwell_watch_index,
    build_dwell_watch_index,
)
from app.geofence_transition_state_store import (
    GeofenceTransitionStateRecord,
    list_geofence_transition_states,
    upsert_geofence_transition_state,
)
from app.location_history_retention import LocationHistoryRetention
from app.location_monitoring_policy import LocationMonitoringPolicy
from app.location_report import location_epoch_to_iso_z
from app.location_request_coordinator import (
    DeferredAccuracyEdgeSnapshot,
    LocationRequestContext,
    LocationRequestCoordinator,
)
from app.mytracks_store import load_location_history_retention
from app.presence_connection_type import connection_type_is_wifi
from app.presence_store import (
    UserLocationRecord,
    geofence_ids_containing_location,
    list_user_location_history_for_user,
    list_user_location_history_for_walkback_by_user,
    list_user_locations,
)
from app.rule_actions import (
    RuleActionDispatchError,
    RuleDeviceDispatchResult,
    RuleNotificationEmailOutcome,
    dispatch_rule_device_actions,
    partition_device_actions_by_delay,
    send_rule_notification_email,
)
from app.rule_conditions import (
    LOCATION_HISTORY_WALKBACK_MAX_S,
    RuleEvaluationContext,
    RuleEvaluationResult,
    _evaluate_condition,
    compute_rules_sun_out,
    consume_scheduled_dwell_episodes_for_fire,
    desired_bool_for_device_condition_state,
    dwell_episode_blocks_fire,
    evaluate_rule,
    iter_dwell_for_s_conditions,
    natural_bool_for_device_family,
    presence_user_ids_for_rule,
)
from app.rule_fire_state_store import list_rule_fire_states, upsert_rule_fire_state
from app.rule_validation import (
    build_roster_user_id_lookup,
    collect_rule_user_ids,
    resolve_device_ref_to_backend_id,
    rule_references_user_id,
    rule_watches_backend_device,
)
from app.rules_store import GeofenceRecord, list_geofences, list_users
from app.vacation_mode import tick_vacation_mode
from app.wifi_home_presence import (
    history_row_geofence_inside,
    wifi_home_geofence_ids,
    wifi_home_presence_applies,
)

_LOGGER = logging.getLogger(__name__)
_GEOFENCE_SEED_MAX_HISTORY_LOOKBACK_S = 86_400.0 * 7
_GEO_INSIDE_STATE_RECONCILE_S = 600.0
_GEO_OUTSIDE_STATE_RECONCILE_S = 600.0
# Retry cadence when a due delayed action can't dispatch yet because device
# discovery has not finished (e.g. shortly after a restart) — the action is kept
# queued rather than dropped so restart survival holds.
_DEFERRED_DEVICE_ACTION_DISCOVERY_RETRY_S = 5.0
_MAX_DEVICE_ACTION_DELAY_S = 86_400  # document; schema enforces
_MIN_GEOFENCE_OUTSIDE_DWELL_S = 300.0
_RULE_EVALUATOR_TICK_S = 60.0
DeferredGeofenceEvent = Literal["entered", "left"]
RuleFireSource = Literal[
    "deferred",
    "device_state",
    "dwell_satisfied",
    "eligibility",
    "immediate",
    "scheduled",
]


@dataclass(frozen=True)
class _DeferredAccuracyEdge:
    event: DeferredGeofenceEvent
    expires_at: float
    geofence_id: str
    observed_at: float
    rule_id: str
    user_id: str


@dataclass(frozen=True)
class _DeferredDeviceAction:
    action: RuleDeviceActionOut
    due_at: float
    fire_at: float
    rule_id: str
    # SQLite row id when persisted; None when no discovery cache is configured.
    row_id: int | None = None


@dataclass(frozen=True)
class GeofenceTransition:
    entered: bool = False
    left: bool = False


@dataclass(frozen=True)
class RuleEvaluatorFireState:
    effective_schedule_cron: str | None = None
    last_error: str | None = None
    last_fired_at: float | None = None
    next_evaluate_at: float | None = None


@dataclass
class _RuleRuntimeState:
    effective_schedule_cron: str | None = None
    last_error: str | None = None
    last_fired_at: float | None = None
    next_evaluate_at: float | None = None
    schedule_materialized_for: date | None = None


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
        # Delayed device_actions queue; mirrored to SQLite so it survives restart.
        self._deferred_device_actions: list[_DeferredDeviceAction] = []
        self._deferred_device_actions_task: asyncio.Task[None] | None = None
        self._deferred_device_actions_wake = asyncio.Event()
        # Natural bool streak per backend device (on/open/playing vs off/closed/paused).
        self._device_bool_since: dict[tuple[DeviceFamilyId, str], float] = {}
        self._device_bool_value: dict[tuple[DeviceFamilyId, str], bool] = {}
        # Ephemeral per-rule ``since`` epoch already evaluated for device-state
        # ``dwell_satisfied`` this streak — cleared when the device bool flips.
        self._device_dwell_satisfied_evaluated_since: dict[
            tuple[str, DeviceFamilyId, str, DeviceConditionState, int],
            float,
        ] = {}
        # Ephemeral per-rule ``since`` epoch already evaluated for
        # ``dwell_satisfied`` this streak — avoids re-running full rule checks on
        # every location ping. Cleared when the geofence streak side resets (enter,
        # leave, or a newer location retargets ``inside_since`` / ``outside_since``).
        # After restart, ``_seed_dwell_satisfied_eval_debounce`` warms slots for
        # episodes already fired or consumed; other debounce state is rebuilt on
        # the next location ingest.
        self._dwell_satisfied_evaluated_since: dict[
            tuple[str, str, str, DwellDirection, int],
            float,
        ] = {}
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._geofence_geo_inside_streak_since: dict[tuple[str, str], float] = {}
        self._geofence_geo_outside_streak_since: dict[tuple[str, str], float] = {}
        self._geofence_inside_since: dict[tuple[str, str], float] = {}
        self._geofence_outside_since: dict[tuple[str, str], float] = {}
        self._geofence_presence_episode: dict[tuple[str, str], int] = {}
        self._geofence_was_inside: dict[tuple[str, str], bool] = {}
        self._last_run_at: float | None = None
        self._last_astronomical_materialization_date: date | None = None
        self._next_sun_check_at: float | None = None
        self._process_lock = asyncio.Lock()
        self._rule_state: dict[str, _RuleRuntimeState] = {}
        self._location_request_coordinator = LocationRequestCoordinator(
            cache_path=cache_path,
            now_fn=self._now_fn,
        )
        self._location_monitoring = LocationMonitoringPolicy(
            cache_path=cache_path,
            coordinator=self._location_request_coordinator,
            deferred_edges_for_user=self.deferred_accuracy_edge_snapshots_for_user,
            now_fn=self._now_fn,
        )
        self._location_request_coordinator._on_location_request_throttled = (
            self._location_monitoring.record_approach_request_throttled
        )
        self._scheduled_inside_dwell_consumed: dict[tuple[str, str, str], int] = {}
        self._scheduled_outside_dwell_consumed: dict[tuple[str, str, str], int] = {}
        self._stop = asyncio.Event()
        self._tick_task: asyncio.Task[None] | None = None
        self._load_persisted_rule_state()
        self._seed_geofence_state()
        self._seed_scheduled_dwell_consumed_from_persisted_fire()
        self._seed_dwell_satisfied_eval_debounce()
        self._seed_scheduled_evaluate_times()
        self._seed_deferred_device_actions()

    async def close(self) -> None:
        self._stop.set()
        self._deferred_device_actions_wake.set()
        await self._location_monitoring.close()
        if self._deferred_device_actions_task is not None and not self._deferred_device_actions_task.done():
            self._deferred_device_actions_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._deferred_device_actions_task
        self._deferred_device_actions_task = None
        self._deferred_device_actions.clear()
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tick_task
        self._tick_task = None

    def deferred_accuracy_edge_snapshots_for_user(
        self,
        user_id: str,
    ) -> tuple[DeferredAccuracyEdgeSnapshot, ...]:
        """Return pending deferred accuracy edges for ``user_id``."""
        snapshots: list[DeferredAccuracyEdgeSnapshot] = []
        for deferred in self._deferred_accuracy_edges.values():
            if deferred.user_id != user_id:
                continue
            snapshots.append(
                DeferredAccuracyEdgeSnapshot(
                    event=deferred.event,
                    expires_at=deferred.expires_at,
                    geofence_id=deferred.geofence_id,
                    observed_at=deferred.observed_at,
                    rule_id=deferred.rule_id,
                    user_id=deferred.user_id,
                )
            )
        return tuple(snapshots)

    def device_bool_since_snapshot(self) -> dict[tuple[DeviceFamilyId, str], float]:
        """Return a copy of device natural-bool streak start times."""
        return dict(self._device_bool_since)

    def device_bool_value_snapshot(self) -> dict[tuple[DeviceFamilyId, str], bool]:
        """Return a copy of device natural-bool streak values."""
        return dict(self._device_bool_value)

    def fire_state_for_rule(self, rule_id: str) -> RuleEvaluatorFireState:
        state = self._rule_state.get(rule_id)
        if state is None:
            return RuleEvaluatorFireState()
        return RuleEvaluatorFireState(
            effective_schedule_cron=state.effective_schedule_cron,
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

    def geofence_presence_episode_snapshot(self) -> dict[tuple[str, str], int]:
        """Return a copy of geofence presence episode counters keyed by user and geofence."""
        return dict(self._geofence_presence_episode)

    @property
    def last_run_at(self) -> float | None:
        return self._last_run_at

    def next_evaluate_at_for_rule(self, rule_id: str) -> float | None:
        state = self._rule_state.get(rule_id)
        if state is None:
            return None
        return state.next_evaluate_at

    def effective_schedule_cron_for_rule(self, rule_id: str) -> str | None:
        state = self._rule_state.get(rule_id)
        if state is None:
            return None
        return state.effective_schedule_cron

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
            asyncio.get_running_loop()
        except RuntimeError:
            event_loop = self._event_loop
            if event_loop is None:
                _LOGGER.warning(
                    "[rules] dropped location update for user_id=%s: no event loop registered yet",
                    user_id,
                )
                return
            try:
                event_loop.call_soon_threadsafe(
                    self._schedule_location_update_task,
                    user_id,
                )
            except RuntimeError:
                _LOGGER.warning(
                    "[rules] dropped location update for user_id=%s: event loop is closed",
                    user_id,
                )
            return
        self._schedule_location_update_task(user_id)

    async def on_device_state_change(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        if self._cache_path is None:
            return
        trimmed = device_id.strip()
        if trimmed == "":
            return
        async with self._process_lock:
            await self._process_device_state_change(family_id, trimmed)

    def schedule_device_state_change(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            event_loop = self._event_loop
            if event_loop is None:
                _LOGGER.warning(
                    "[rules] dropped device-state update for %s/%s: no event loop registered yet",
                    family_id.value,
                    device_id,
                )
                return
            try:
                event_loop.call_soon_threadsafe(
                    self._schedule_device_state_change_task,
                    family_id,
                    device_id,
                )
            except RuntimeError:
                _LOGGER.warning(
                    "[rules] dropped device-state update for %s/%s: event loop is closed",
                    family_id.value,
                    device_id,
                )
            return
        self._schedule_device_state_change_task(family_id, device_id)

    def start_periodic_tick(self) -> None:
        if self._tick_task is not None:
            return
        self._event_loop = asyncio.get_running_loop()
        self._tick_task = asyncio.create_task(
            self._periodic_loop(),
            name="rule-evaluator-tick",
        )
        if self._deferred_device_actions_task is None:
            self._deferred_device_actions_task = asyncio.create_task(
                self._deferred_device_actions_loop(),
                name="rule-evaluator-deferred-device-actions",
            )
        self._location_monitoring.start_background_loops()

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
        user_home_wifi_bssid: dict[str, str | None] = {}
        user_location_history: dict[str, tuple[UserLocationOut, ...]] = {}
        user_locations: dict[str, UserLocationOut] = {}
        if cache_path is not None:
            geofences = [_geofence_record_to_out(row) for row in list_geofences(cache_path)]
            users = list_users(cache_path)
            user_display_names = {row.user_id: row.display_name for row in users}
            user_home_wifi_bssid = {row.user_id: row.home_wifi_bssid for row in users}
            stored = list_user_locations(cache_path)
            now_epoch = effective_now.timestamp()
            walkback_max_s = LOCATION_HISTORY_WALKBACK_MAX_S
            walkback_by_user = list_user_location_history_for_walkback_by_user(
                cache_path,
                list(stored.keys()),
                now_epoch=now_epoch,
                walkback_max_s=walkback_max_s,
            )
            for uid, location in stored.items():
                user_locations[uid] = UserLocationOut(
                    accuracy_m=location.accuracy_m,
                    battery_level=location.battery_level,
                    connection_type=location.connection_type,
                    fix_source=location.fix_source,
                    fix_at=_location_fix_at_iso(location),
                    lat=location.lat,
                    lon=location.lon,
                    reported_at=_location_reported_at_iso(location),
                    source=location.source,
                    trigger=location.trigger,
                    wifi_bssid=location.wifi_bssid,
                    wifi_ssid=location.wifi_ssid,
                )
                history_rows = walkback_by_user.get(uid, ())
                user_location_history[uid] = tuple(
                    UserLocationOut(
                        accuracy_m=row.accuracy_m,
                        battery_level=row.battery_level,
                        connection_type=row.connection_type,
                        fix_source=row.fix_source,
                        fix_at=_location_fix_at_iso(row),
                        lat=row.lat,
                        lon=row.lon,
                        reported_at=_location_reported_at_iso(row),
                        source=row.source,
                        trigger=row.trigger,
                        wifi_bssid=row.wifi_bssid,
                        wifi_ssid=row.wifi_ssid,
                    )
                    for row in history_rows
                )
        roster_user_id_lookup = build_roster_user_id_lookup(list(user_display_names))
        return RuleEvaluationContext(
            geofences=tuple(geofences),
            now=effective_now,
            roster_user_id_lookup=roster_user_id_lookup,
            sun=sun,
            timezone=tz,
            user_display_names=user_display_names,
            user_home_wifi_bssid=user_home_wifi_bssid,
            user_locations=user_locations,
            device_bool_since=self.device_bool_since_snapshot(),
            device_bool_value=self.device_bool_value_snapshot(),
            device_state=self._device_state_getter(),
            geofence_inside_since=self.geofence_inside_since_snapshot(),
            geofence_outside_since=self.geofence_outside_since_snapshot(),
            geofence_presence_episode=self.geofence_presence_episode_snapshot(),
            scheduled_inside_dwell_consumed_episode=dict(
                self._scheduled_inside_dwell_consumed,
            ),
            scheduled_outside_dwell_consumed_episode=dict(
                self._scheduled_outside_dwell_consumed,
            ),
            user_location_history=user_location_history,
            walkback_max_s=walkback_max_s,
        )

    def _advance_scheduled_evaluate_time(
        self,
        rule: RuleOut,
        *,
        timezone: ZoneInfo,
    ) -> None:
        runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone)
        if uses_astronomical_repeat_schedule(rule):
            settings = load_settings_location()
            runtime.next_evaluate_at = next_astronomical_repeat_evaluate_at(
                rule,
                settings=settings,
                timezone=timezone,
                now=now,
            )
            self._persist_rule_schedule_state(rule.id)
            return
        cron_expr = self._resolve_schedule_cron(rule, timezone=timezone)
        if cron_expr == "":
            return
        runtime.next_evaluate_at = next_scheduled_evaluate_at(
            cron_expr,
            now,
            timezone,
        )
        if uses_astronomical_materialized_schedule(rule):
            self._persist_rule_schedule_state(rule.id)

    def _apply_persisted_geofence_state(self) -> None:
        """Load geofence transition maps from SQLite persistence."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        for record in list_geofence_transition_states(cache_path).values():
            key = (record.user_id, record.geofence_id)
            self._geofence_was_inside[key] = record.was_inside
            if record.outside_since is not None:
                self._set_geofence_outside_since(key, record.outside_since)
            if record.inside_since is not None:
                self._set_geofence_inside_since(key, record.inside_since)

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
        home_wifi_bssid = _home_wifi_bssid_for_user(self._cache_path, user_id)
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
                home_wifi_bssid=home_wifi_bssid,
                observed_wifi_bssid=location.wifi_bssid,
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
                self._drop_geofence_outside_since(key)
                self._bump_geofence_presence_episode(user_id, geofence_id)
                _log_wifi_home_presence_reconciled(
                    user_id=user_id,
                    geofence_id=geofence_id,
                    connection_type=location.connection_type,
                )
            if dwell_accuracy_limit_m is not None and key not in self._geofence_inside_since:
                self._set_geofence_inside_since(key, observed_at)
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
        self._drop_geofence_outside_since(key)
        self._drop_geofence_inside_since(key)
        history = list_user_location_history_for_user(
            cache_path,
            user_id,
            since=history_since,
        )
        settings = load_settings_location()
        geofence_list = list_geofences(cache_path)
        home_wifi_bssid = _home_wifi_bssid_for_user(cache_path, user_id)
        seeded = False
        if history:
            was_inside, outside_since, inside_since = _reconstruct_geofence_seed_from_history(
                geofence,
                history,
                dwell_accuracy_limit_m=dwell_accuracy_limit_m,
                edge_accuracy_limit_m=edge_accuracy_limit_m,
                geofences=geofence_list,
                settings=settings,
                user_id=user_id,
                home_wifi_bssid=home_wifi_bssid,
            )
            if was_inside is not None:
                self._geofence_was_inside[key] = was_inside
                seeded = True
                if not was_inside and outside_since is not None:
                    self._set_geofence_outside_since(key, outside_since)
            if inside_since is not None:
                self._set_geofence_inside_since(key, inside_since)
                seeded = True
        if not seeded:
            edge_inside = history_row_geofence_inside(
                location,
                geofence,
                geofence_list,
                settings=settings,
                min_accuracy_m=edge_accuracy_limit_m,
                home_wifi_bssid=home_wifi_bssid,
            )
            dwell_inside = history_row_geofence_inside(
                location,
                geofence,
                geofence_list,
                settings=settings,
                min_accuracy_m=dwell_accuracy_limit_m,
                home_wifi_bssid=home_wifi_bssid,
            )
            if edge_inside is not None:
                self._geofence_was_inside[key] = edge_inside
                seeded = True
            if dwell_inside and dwell_accuracy_limit_m is not None:
                self._set_geofence_inside_since(key, location.reported_at)
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
                    home_wifi_bssid=home_wifi_bssid,
                    observed_wifi_bssid=location.wifi_bssid,
                ):
                    _log_wifi_home_presence_overrode_low_accuracy(
                        user_id=user_id,
                        geofence_id=geofence_id,
                        accuracy_m=location.accuracy_m,
                        threshold_m=dwell_accuracy_limit_m,
                    )
            elif dwell_inside is False and dwell_accuracy_limit_m is not None:
                self._set_geofence_outside_since(key, location.reported_at)
                seeded = True
            if seeded:
                self._persist_geofence_transition_state(
                    user_id,
                    geofence_id,
                    last_location_received_at=location.reported_at,
                )

    def _bump_geofence_presence_episode(self, user_id: str, geofence_id: str) -> int:
        key = (user_id, geofence_id)
        episode = self._geofence_presence_episode.get(key, 0) + 1
        self._geofence_presence_episode[key] = episode
        return episode

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
            if not rule.enabled or not _rule_uses_scheduled_evaluation_tick(rule):
                continue
            cron_expr = self._resolve_schedule_cron(rule, timezone=timezone)
            if cron_expr == "":
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if runtime.next_evaluate_at is None:
                if uses_astronomical_repeat_schedule(rule):
                    runtime.next_evaluate_at = next_astronomical_repeat_evaluate_at(
                        rule,
                        settings=settings,
                        timezone=timezone,
                        now=now,
                        due_if_inside_window=True,
                    )
                else:
                    runtime.next_evaluate_at = next_scheduled_evaluate_at(
                        cron_expr,
                        now,
                        timezone,
                        due_if_matching=True,
                    )
            if runtime.next_evaluate_at > now_epoch:
                continue
            eligibility_wake = uses_astronomical_eligibility_wake(rule)
            fire_source: RuleFireSource = "eligibility" if eligibility_wake else "scheduled"
            log_user_ids = _scheduled_rule_user_ids_for_log(rule, ctx)
            evaluation_ctx = replace(
                ctx,
                triggered_by=(RuleEvaluationCause.ELIGIBILITY if eligibility_wake else RuleEvaluationCause.SCHEDULED),
            )
            evaluation = evaluate_rule(rule, evaluation_ctx)
            _LOGGER.info(
                "[rules] %s evaluate rule_id=%s met=%s",
                fire_source,
                rule.id,
                evaluation.all_met,
            )
            if evaluation.all_met:
                if self._skip_if_daily_cap(
                    log_user_ids=log_user_ids,
                    now_epoch=now_epoch,
                    rule=rule,
                    runtime=runtime,
                    timezone=timezone,
                ):
                    pass
                elif dwell_episode_blocks_fire(rule, ctx):
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="dwell_episode_consumed",
                        detail="dwell already fired this away/inside episode",
                    )
                elif self._cooldown_elapsed(rule, runtime):
                    await self._execute_rule(
                        rule,
                        evaluation=evaluation,
                        fire_source=fire_source,
                        log_user_ids=log_user_ids,
                        transitions={},
                    )
                else:
                    remaining_s = rule.cooldown_s - (now_epoch - (runtime.last_fired_at or 0.0))
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
        keys_for_user = [key for key in self._deferred_accuracy_edges if key[1] == user_id]
        for key in keys_for_user:
            deferred = self._deferred_accuracy_edges.get(key)
            if deferred is None or now > deferred.expires_at:
                continue
            rule = _automation_rule_by_id(deferred.rule_id)
            if rule is None or not rule.enabled or RuleTrigger.EDGE_TRUE not in rule.triggers:
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
            if self._skip_if_daily_cap(
                log_user_ids=user_id,
                now_epoch=now,
                rule=rule,
                runtime=runtime,
                timezone=ctx.timezone,
            ):
                continue
            if not self._cooldown_elapsed(rule, runtime):
                remaining_s = rule.cooldown_s - (now - (runtime.last_fired_at or 0.0))
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

    def _cancel_deferred_device_actions_for_rule(self, rule_id: str) -> None:
        had_entries = any(entry.rule_id == rule_id for entry in self._deferred_device_actions)
        self._deferred_device_actions = [entry for entry in self._deferred_device_actions if entry.rule_id != rule_id]
        if had_entries and self._cache_path is not None:
            delete_deferred_device_actions_for_rule(self._cache_path, rule_id)

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
        keys = [key for key in self._deferred_accuracy_edges if key[0] == rule_id and key[1] == user_id]
        for key in keys:
            self._deferred_accuracy_edges.pop(key, None)

    def _cooldown_elapsed(self, rule: RuleOut, state: _RuleRuntimeState) -> bool:
        if state.last_fired_at is None:
            return True
        return self._now_fn() - state.last_fired_at >= rule.cooldown_s

    async def _deferred_device_actions_loop(self) -> None:
        while not self._stop.is_set():
            timeout: float | None = None
            async with self._process_lock:
                self._prune_stale_deferred_device_actions()
                now = self._now_fn()
                due = [entry for entry in self._deferred_device_actions if entry.due_at <= now]
                remaining = [entry for entry in self._deferred_device_actions if entry.due_at > now]
                device_state = self._device_state_getter()
                if due and device_state is None:
                    # Discovery has not finished yet (e.g. shortly after a
                    # restart mid-delay). Keep due actions queued and retry soon
                    # so persisted follow-ups still run once discovery completes.
                    _LOGGER.warning(
                        "[rules] %d delayed device action(s) waiting for device discovery to finish",
                        len(due),
                    )
                    self._deferred_device_actions = due + remaining
                    timeout = _DEFERRED_DEVICE_ACTION_DISCOVERY_RETRY_S
                else:
                    self._deferred_device_actions = remaining
                    dispatched_row_ids: list[int] = []
                    for entry in due:
                        # Reached only when device discovery is ready: the
                        # ``due and device_state is None`` guard above keeps due
                        # actions queued while ``device_state`` is None.
                        assert device_state is not None
                        if entry.row_id is not None:
                            dispatched_row_ids.append(entry.row_id)
                        delay_s = entry.action.delay_s
                        if delay_s is None:
                            delay_s = int(max(0.0, entry.due_at - entry.fire_at))
                        dispatch_result = await dispatch_rule_device_actions(
                            device_state,
                            [entry.action],
                        )
                        if dispatch_result.errors:
                            _LOGGER.warning(
                                "[rules] delayed device action failed rule_id=%s fire_at=%s "
                                "delay_s=%s family_id=%s device_id=%s action=%s errors=%s",
                                entry.rule_id,
                                location_epoch_to_iso_z(entry.fire_at),
                                delay_s,
                                entry.action.family_id.value,
                                entry.action.device_id,
                                entry.action.action.value,
                                "; ".join(dispatch_result.errors),
                            )
                        else:
                            _LOGGER.info(
                                "[rules] delayed device action dispatched rule_id=%s fire_at=%s "
                                "delay_s=%s family_id=%s device_id=%s action=%s",
                                entry.rule_id,
                                location_epoch_to_iso_z(entry.fire_at),
                                delay_s,
                                entry.action.family_id.value,
                                entry.action.device_id,
                                entry.action.action.value,
                            )
                    self._delete_persisted_deferred_device_actions(dispatched_row_ids)
                if timeout is None and self._deferred_device_actions:
                    next_due = min(entry.due_at for entry in self._deferred_device_actions)
                    timeout = max(0.0, next_due - self._now_fn())

            self._deferred_device_actions_wake.clear()
            wake_task = asyncio.create_task(self._deferred_device_actions_wake.wait())
            stop_task = asyncio.create_task(self._stop.wait())
            try:
                done, pending = await asyncio.wait(
                    {wake_task, stop_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                for task in done:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            except asyncio.CancelledError:
                wake_task.cancel()
                stop_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await wake_task
                with contextlib.suppress(asyncio.CancelledError):
                    await stop_task
                raise

    def _delete_persisted_deferred_device_actions(self, row_ids: list[int]) -> None:
        if not row_ids or self._cache_path is None:
            return
        delete_deferred_device_actions(self._cache_path, row_ids)

    def _enqueue_deferred_device_actions(
        self,
        rule_id: str,
        fire_at: float,
        delayed: list[RuleDeviceActionOut],
    ) -> None:
        enqueued_any = False
        for action in delayed:
            delay_s = action.delay_s
            if delay_s is None or delay_s <= 0:
                continue
            capped_delay_s = min(delay_s, _MAX_DEVICE_ACTION_DELAY_S)
            due_at = fire_at + float(capped_delay_s)
            row_id: int | None = None
            if self._cache_path is not None:
                row_id = insert_deferred_device_action(
                    self._cache_path,
                    action=action,
                    due_at=due_at,
                    fire_at=fire_at,
                    rule_id=rule_id,
                )
            self._deferred_device_actions.append(
                _DeferredDeviceAction(
                    action=action,
                    due_at=due_at,
                    fire_at=fire_at,
                    rule_id=rule_id,
                    row_id=row_id,
                )
            )
            enqueued_any = True
        if enqueued_any:
            self._deferred_device_actions_wake.set()

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
        immediate, delayed = partition_device_actions_by_delay(rule.device_actions)
        performed_side_effect = not rule.device_actions and not rule.notify_on_fire
        device_state = self._device_state_getter()
        dispatch_result = RuleDeviceDispatchResult.empty()
        if device_state is None:
            if immediate or delayed:
                errors.append("Device discovery still in progress; actions skipped")
        else:
            if immediate:
                dispatch_result = await dispatch_rule_device_actions(
                    device_state,
                    immediate,
                )
                errors.extend(dispatch_result.errors)
                probable_successes = list(dispatch_result.probable_successes)
                if not dispatch_result.errors:
                    performed_side_effect = True
            if delayed:
                self._enqueue_deferred_device_actions(rule.id, self._now_fn(), delayed)
                performed_side_effect = True
        if rule.notify_on_fire and self._cache_path is not None:
            try:
                ctx = await self._build_evaluation_context(
                    now=datetime.fromtimestamp(self._now_fn(), tz=UTC),
                )
                email_outcome = await asyncio.to_thread(
                    send_rule_notification_email,
                    self._cache_path,
                    device_action_outcomes=dispatch_result.action_outcomes,
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
        if fire_source in ("device_state", "dwell_satisfied", "eligibility", "scheduled") and _rule_has_dwell_condition(
            rule
        ):
            consume_scheduled_dwell_episodes_for_fire(
                rule,
                await self._build_evaluation_context(
                    now=datetime.fromtimestamp(self._now_fn(), tz=UTC),
                ),
                consumed_inside=self._scheduled_inside_dwell_consumed,
                consumed_outside=self._scheduled_outside_dwell_consumed,
            )
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
            _format_rule_conditions_for_log(
                rule,
                evaluation,
                fire_source=fire_source,
            ),
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
                effective_schedule_cron=record.effective_schedule_cron,
                last_error=record.last_error,
                last_fired_at=record.last_fired_at,
                next_evaluate_at=record.next_evaluate_at,
                schedule_materialized_for=parse_schedule_materialized_for(
                    record.schedule_materialized_for,
                ),
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
                    self._prune_stale_deferred_device_actions()
                    self._refresh_astronomical_schedules_for_new_day()
                    await self._evaluate_scheduled_rules()
                    timezone = ZoneInfo(load_settings_location().timezone)
                    ctx = await self._build_evaluation_context(
                        now=datetime.fromtimestamp(self._now_fn(), tz=timezone),
                    )
                    self._sync_all_device_dwell_streaks(ctx, self._now_fn())
                    await self._maybe_process_device_dwell_satisfied()
                    await self._maybe_request_locations_for_deferred_edges()
                    await self._tick_vacation_mode(ctx, now=self._now_fn())
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

    def _prune_stale_deferred_device_actions(self) -> None:
        if not self._deferred_device_actions:
            return
        try:
            rules_by_id = {rule.id: rule for rule in list_automation_rules()}
        except Exception:
            _LOGGER.exception("[rules] failed to load rules while pruning deferred device actions")
            return
        stale_rule_ids = {
            entry.rule_id
            for entry in self._deferred_device_actions
            if (rule := rules_by_id.get(entry.rule_id)) is None or not rule.enabled
        }
        for rule_id in stale_rule_ids:
            self._cancel_deferred_device_actions_for_rule(rule_id)

    async def _tick_vacation_mode(
        self,
        ctx: RuleEvaluationContext,
        *,
        now: float,
    ) -> None:
        """Advance the vacation-mode latch from periodic and location ticks.

        Runs the tick (including possible SMTP) via ``asyncio.to_thread`` so a
        slow mail server does not block the evaluator event loop.
        """
        cache_path = self._cache_path
        if cache_path is None:
            return
        try:
            settings = load_vacation_mode_settings()
        except Exception:
            _LOGGER.exception("[vacation] failed to load vacation_mode settings")
            return
        try:
            await asyncio.to_thread(
                tick_vacation_mode,
                cache_path,
                ctx=ctx,
                now=now,
                settings=settings,
            )
        except Exception:
            _LOGGER.exception("[vacation] latch tick failed")

    def _persist_rule_schedule_state(self, rule_id: str) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        state = self._rule_state.get(rule_id)
        if state is None:
            return
        materialized_for = (
            schedule_materialized_for_date(state.schedule_materialized_for)
            if state.schedule_materialized_for is not None
            else None
        )
        upsert_rule_fire_state(
            cache_path,
            effective_schedule_cron=state.effective_schedule_cron,
            last_error=state.last_error,
            last_fired_at=state.last_fired_at,
            next_evaluate_at=state.next_evaluate_at,
            rule_id=rule_id,
            schedule_materialized_for=materialized_for,
            update_schedule_fields=True,
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
                observed_at=location.reported_at,
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
            self._set_geofence_outside_since(key, history_outside_since)
            self._persist_geofence_transition_state(
                user_id,
                geofence_id,
                last_location_received_at=location.reported_at,
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
            observed_at=location.reported_at,
        )
        inside_ids = set(geofence_ids_containing_location(location, geofences))
        now = self._now_fn()
        expired_deferred_keys = self._expire_deferred_accuracy_edges(user_id, now)
        if transitions:
            transitions_summary = _format_geofence_transitions_for_log(transitions)
            _LOGGER.debug(
                "[rules] evaluating location update user_id=%s lat=%.5f lon=%.5f accuracy_m=%s transitions=%s",
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
            if not rule.enabled or RuleTrigger.EDGE_TRUE not in rule.triggers:
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
                        detail=(f"accuracy_m={location.accuracy_m} limit={rule.min_location_accuracy_m}"),
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
            if self._skip_if_daily_cap(
                log_user_ids=user_id,
                now_epoch=now,
                rule=rule,
                runtime=runtime,
                timezone=ctx.timezone,
            ):
                continue
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
        self._location_request_coordinator.maybe_request(
            user_id,
            context=LocationRequestContext(
                deferred_edges=self.deferred_accuracy_edge_snapshots_for_user(user_id),
                location=location,
                now=now,
            ),
        )
        self._location_monitoring.on_location_updated(
            user_id,
            location=location,
            now=now,
        )
        await self._maybe_process_dwell_satisfied(user_id)
        await self._tick_vacation_mode(ctx, now=now)
        self._last_run_at = now
        self._next_sun_check_at = self._last_run_at + _RULE_EVALUATOR_TICK_S

    async def _maybe_request_locations_for_deferred_edges(self) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        now = self._now_fn()
        user_ids = {deferred.user_id for deferred in self._deferred_accuracy_edges.values()}
        if not user_ids:
            return
        locations = list_user_locations(cache_path)
        for user_id in sorted(user_ids):
            location = locations.get(user_id)
            if location is None:
                continue
            self._location_request_coordinator.maybe_request(
                user_id,
                context=LocationRequestContext(
                    deferred_edges=self.deferred_accuracy_edge_snapshots_for_user(user_id),
                    location=location,
                    now=now,
                ),
            )

    def _seed_deferred_device_actions(self) -> None:
        """Reload persisted delayed device actions so they survive a restart.

        Rows whose ``due_at`` already elapsed while the process was down are
        dispatched promptly on the first drain; stale rows for disabled/removed
        rules are pruned before their first dispatch.
        """
        cache_path = self._cache_path
        if cache_path is None:
            return
        try:
            records = list_deferred_device_actions(cache_path)
        except Exception:
            _LOGGER.exception("[rules] failed to reload persisted delayed device actions")
            return
        if not records:
            return
        self._deferred_device_actions = [
            _DeferredDeviceAction(
                action=record.action,
                due_at=record.due_at,
                fire_at=record.fire_at,
                rule_id=record.rule_id,
                row_id=record.row_id,
            )
            for record in records
        ]
        _LOGGER.info(
            "[rules] reloaded %d persisted delayed device action(s) after restart",
            len(self._deferred_device_actions),
        )

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
            if not rule.enabled or not _rule_uses_scheduled_evaluation_tick(rule):
                continue
            if uses_astronomical_materialized_schedule(rule):
                self._ensure_astronomical_schedule_materialized(
                    rule,
                    timezone=timezone,
                    now=now,
                )
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

    def _resolve_schedule_cron(
        self,
        rule: RuleOut,
        *,
        timezone: ZoneInfo,
    ) -> str:
        repeat_cron = astronomical_repeat_cron(rule)
        if uses_astronomical_materialized_schedule(rule) and repeat_cron is not None:
            return repeat_cron
        if uses_astronomical_materialized_schedule(rule):
            now = datetime.fromtimestamp(self._now_fn(), tz=timezone)
            materialized = self._ensure_astronomical_schedule_materialized(
                rule,
                timezone=timezone,
                now=now,
            )
            return materialized or ""
        return (rule.schedule_cron or "").strip()

    def _schedule_location_update_task(self, user_id: str) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self.on_location_update(user_id),
            name=f"rule-eval-location-{user_id}",
        )
        task.add_done_callback(_log_location_evaluation_task)

    def _schedule_device_state_change_task(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        loop = asyncio.get_running_loop()
        task = loop.create_task(
            self.on_device_state_change(family_id, device_id),
            name=f"rule-eval-device-{family_id.value}-{device_id}",
        )
        task.add_done_callback(_log_device_state_evaluation_task)

    async def _maybe_process_device_dwell_satisfied(
        self,
        family_id: DeviceFamilyId | None = None,
        backend_device_id: str | None = None,
    ) -> None:
        rules = list_automation_rules()
        index = build_device_dwell_watch_index(rules)
        if not index.watches:
            return
        now_epoch = self._now_fn()
        timezone = ZoneInfo(load_settings_location().timezone)
        now = datetime.fromtimestamp(now_epoch, tz=timezone)
        ctx = await self._build_evaluation_context(now=now)
        device_state = ctx.device_state
        if device_state is None:
            return
        watches: tuple[DeviceDwellWatch, ...]
        if family_id is not None and backend_device_id is not None:
            watches = index.watches_for_backend_device(
                family_id=family_id,
                backend_device_id=backend_device_id,
                matches_ref=lambda fid, bid, ref: (
                    resolve_device_ref_to_backend_id(
                        device_state,
                        family_id=fid,
                        device_ref=ref,
                    )
                    == bid
                    or ref.strip() == bid.strip()
                ),
            )
        else:
            watches = index.watches
        crossed_rule_ids: set[str] = set()
        newly_evaluated_rules: list[
            tuple[
                tuple[str, DeviceFamilyId, str, DeviceConditionState, int],
                float,
            ]
        ] = []
        rules_by_id = {rule.id: rule for rule in rules}
        for watch in watches:
            if family_id is not None and backend_device_id is not None:
                streak_backend_id = backend_device_id
            else:
                resolved = resolve_device_ref_to_backend_id(
                    device_state,
                    family_id=watch.family_id,
                    device_ref=watch.device_id,
                )
                if resolved is None:
                    continue
                streak_backend_id = resolved
            streak_key = (watch.family_id, streak_backend_id)
            since = self._device_bool_since.get(streak_key)
            value = self._device_bool_value.get(streak_key)
            desired = desired_bool_for_device_condition_state(watch.state)
            if since is None or value is None or value != desired or (now_epoch - since) < watch.min_duration_s:
                to_drop = [
                    key
                    for key in self._device_dwell_satisfied_evaluated_since
                    if key[1] == watch.family_id
                    and key[2] == streak_backend_id
                    and key[3] == watch.state
                    and key[4] == watch.min_duration_s
                ]
                for key in to_drop:
                    del self._device_dwell_satisfied_evaluated_since[key]
                continue
            for rule_id in sorted(watch.rule_ids):
                rule_key = (
                    rule_id,
                    watch.family_id,
                    streak_backend_id,
                    watch.state,
                    watch.min_duration_s,
                )
                if self._device_dwell_satisfied_evaluated_since.get(rule_key) == since:
                    continue
                crossed_rule_ids.add(rule_id)
                newly_evaluated_rules.append((rule_key, since))
        if not crossed_rule_ids:
            return
        await self._process_dwell_satisfied_rules(
            crossed_rule_ids,
            rules=rules,
            ctx=ctx,
            now_epoch=now_epoch,
            timezone=timezone,
        )
        for rule_key, since in newly_evaluated_rules:
            rule_id = rule_key[0]
            rule = rules_by_id.get(rule_id)
            if rule is None:
                continue
            runtime = self._rule_state.get(rule_id)
            if dwell_episode_blocks_fire(rule, ctx):
                self._device_dwell_satisfied_evaluated_since[rule_key] = since
                continue
            if runtime is not None and not self._cooldown_elapsed(rule, runtime):
                continue
            self._device_dwell_satisfied_evaluated_since[rule_key] = since

    async def _maybe_process_dwell_satisfied(self, roster_user_id: str) -> None:
        rules = list_automation_rules()
        index = build_dwell_watch_index(rules)
        if not index.watches:
            return
        now_epoch = self._now_fn()
        timezone = ZoneInfo(load_settings_location().timezone)
        now = datetime.fromtimestamp(now_epoch, tz=timezone)
        ctx = await self._build_evaluation_context(now=now)
        crossed_rule_ids: set[str] = set()
        newly_evaluated_rules: list[tuple[tuple[str, str, str, DwellDirection, int], float]] = []
        rules_by_id = {rule.id: rule for rule in rules}
        for watch in index.watches_for_roster_user(
            roster_user_id,
            ctx.resolve_user_id,
        ):
            since = self._dwell_streak_since(
                roster_user_id,
                watch.geofence_id,
                watch.direction,
            )
            if since is None:
                self._clear_dwell_satisfied_eval_for_watch(
                    roster_user_id,
                    watch.geofence_id,
                    watch.direction,
                    watch.min_s,
                )
                continue
            elapsed_s = now_epoch - since
            if elapsed_s < watch.min_s:
                self._clear_dwell_satisfied_eval_for_watch(
                    roster_user_id,
                    watch.geofence_id,
                    watch.direction,
                    watch.min_s,
                )
                continue
            for rule_id in sorted(watch.rule_ids):
                rule_key = (
                    rule_id,
                    roster_user_id,
                    watch.geofence_id,
                    watch.direction,
                    watch.min_s,
                )
                if self._dwell_satisfied_evaluated_since.get(rule_key) == since:
                    continue
                # Do not consume the episode here — only a successful fire does
                # (dwell_episode_blocks_fire). Daytime dwell must not poison a later
                # eligibility wake (e.g. after_sunset) in the same home episode.
                crossed_rule_ids.add(rule_id)
                newly_evaluated_rules.append((rule_key, since))
        if not crossed_rule_ids:
            return
        await self._process_dwell_satisfied_rules(
            crossed_rule_ids,
            rules=rules,
            ctx=ctx,
            now_epoch=now_epoch,
            timezone=timezone,
        )
        for rule_key, since in newly_evaluated_rules:
            rule_id = rule_key[0]
            rule = rules_by_id.get(rule_id)
            if rule is None:
                continue
            runtime = self._rule_state.get(rule_id)
            if dwell_episode_blocks_fire(rule, ctx):
                self._dwell_satisfied_evaluated_since[rule_key] = since
                continue
            if runtime is not None and not self._cooldown_elapsed(rule, runtime):
                continue
            self._dwell_satisfied_evaluated_since[rule_key] = since

    async def _process_device_state_change(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        now_epoch = self._now_fn()
        timezone = ZoneInfo(load_settings_location().timezone)
        now = datetime.fromtimestamp(now_epoch, tz=timezone)
        ctx = await self._build_evaluation_context(now=now)
        self._sync_device_bool_streak(
            family_id,
            device_id,
            now_epoch=now_epoch,
            ctx=ctx,
        )
        evaluation_ctx = replace(
            ctx,
            triggered_by=RuleEvaluationCause.DEVICE_STATE,
        )
        device_state = self._device_state_getter()
        matched_rules: list[RuleOut] = []
        if device_state is not None:
            matched_rules = [
                rule
                for rule in list_automation_rules()
                if rule.enabled
                and RuleTrigger.DEVICE_STATE in rule.triggers
                and rule_watches_backend_device(
                    rule,
                    device_state,
                    family_id=family_id,
                    backend_device_id=device_id,
                )
            ]
        if matched_rules:
            _LOGGER.info(
                "[rules] evaluating device-state change family_id=%s device_id=%s rule_count=%d",
                family_id.value,
                device_id,
                len(matched_rules),
            )
            transitions: dict[str, GeofenceTransition] = {}
            for rule in matched_rules:
                log_user_ids = _scheduled_rule_user_ids_for_log(rule, ctx)
                evaluation = evaluate_rule(rule, evaluation_ctx)
                _LOGGER.info(
                    "[rules] device-state evaluate rule_id=%s met=%s",
                    rule.id,
                    evaluation.all_met,
                )
                if not evaluation.all_met:
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="conditions_not_met",
                        detail=_format_unmet_conditions_for_log(evaluation),
                    )
                    continue
                runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
                if self._skip_if_daily_cap(
                    log_user_ids=log_user_ids,
                    now_epoch=now_epoch,
                    rule=rule,
                    runtime=runtime,
                    timezone=timezone,
                ):
                    continue
                if dwell_episode_blocks_fire(rule, ctx):
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="dwell_episode_consumed",
                        detail="dwell already fired this away/inside episode",
                    )
                    continue
                if not self._cooldown_elapsed(rule, runtime):
                    remaining_s = rule.cooldown_s - (now_epoch - (runtime.last_fired_at or 0.0))
                    _log_rule_skipped(
                        rule.id,
                        log_user_ids,
                        reason="cooldown",
                        detail=f"remaining_s={max(0.0, remaining_s):.0f}",
                    )
                    continue
                await self._execute_rule(
                    rule,
                    evaluation=evaluation,
                    fire_source="device_state",
                    log_user_ids=log_user_ids,
                    transitions=transitions,
                )
        await self._maybe_process_device_dwell_satisfied(family_id, device_id)

    async def _process_dwell_satisfied_rules(
        self,
        rule_ids: set[str],
        *,
        rules: list[RuleOut],
        ctx: RuleEvaluationContext,
        now_epoch: float,
        timezone: ZoneInfo,
    ) -> None:
        rules_by_id = {rule.id: rule for rule in rules if rule.enabled and RuleTrigger.DWELL_SATISFIED in rule.triggers}
        matched_rules = [rules_by_id[rule_id] for rule_id in sorted(rule_ids) if rule_id in rules_by_id]
        if not matched_rules:
            return
        _LOGGER.info(
            "[rules] evaluating dwell-satisfied rule_count=%d",
            len(matched_rules),
        )
        evaluation_ctx = replace(
            ctx,
            triggered_by=RuleEvaluationCause.DWELL,
        )
        transitions: dict[str, GeofenceTransition] = {}
        for rule in matched_rules:
            log_user_ids = _scheduled_rule_user_ids_for_log(rule, ctx)
            evaluation = evaluate_rule(rule, evaluation_ctx)
            _LOGGER.info(
                "[rules] dwell-satisfied evaluate rule_id=%s met=%s",
                rule.id,
                evaluation.all_met,
            )
            if not evaluation.all_met:
                _log_rule_skipped(
                    rule.id,
                    log_user_ids,
                    reason="conditions_not_met",
                    detail=_format_unmet_conditions_for_log(evaluation),
                )
                continue
            runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
            if self._skip_if_daily_cap(
                log_user_ids=log_user_ids,
                now_epoch=now_epoch,
                rule=rule,
                runtime=runtime,
                timezone=timezone,
            ):
                continue
            if dwell_episode_blocks_fire(rule, ctx):
                _log_rule_skipped(
                    rule.id,
                    log_user_ids,
                    reason="dwell_episode_consumed",
                    detail="dwell already fired this away/inside episode",
                )
                continue
            if not self._cooldown_elapsed(rule, runtime):
                remaining_s = rule.cooldown_s - (now_epoch - (runtime.last_fired_at or 0.0))
                _log_rule_skipped(
                    rule.id,
                    log_user_ids,
                    reason="cooldown",
                    detail=f"remaining_s={max(0.0, remaining_s):.0f}",
                )
                continue
            await self._execute_rule(
                rule,
                evaluation=evaluation,
                fire_source="dwell_satisfied",
                log_user_ids=log_user_ids,
                transitions=transitions,
            )

    def _dwell_streak_since(
        self,
        roster_user_id: str,
        geofence_id: str,
        direction: DwellDirection,
    ) -> float | None:
        key = (roster_user_id, geofence_id)
        if direction == DwellDirection.INSIDE:
            return self._geofence_inside_since.get(key)
        return self._geofence_outside_since.get(key)

    def _clear_device_dwell_satisfied_eval_for_device(
        self,
        family_id: DeviceFamilyId,
        backend_device_id: str,
    ) -> None:
        to_drop = [
            key
            for key in self._device_dwell_satisfied_evaluated_since
            if key[1] == family_id and key[2] == backend_device_id
        ]
        for key in to_drop:
            del self._device_dwell_satisfied_evaluated_since[key]

    def _clear_dwell_satisfied_eval_for_geofence_direction(
        self,
        roster_user_id: str,
        geofence_id: str,
        direction: DwellDirection,
    ) -> None:
        to_drop = [
            key
            for key in self._dwell_satisfied_evaluated_since
            if key[1] == roster_user_id and key[2] == geofence_id and key[3] == direction
        ]
        for key in to_drop:
            del self._dwell_satisfied_evaluated_since[key]

    def _clear_dwell_satisfied_eval_for_watch(
        self,
        roster_user_id: str,
        geofence_id: str,
        direction: DwellDirection,
        min_s: int,
    ) -> None:
        suffix = (roster_user_id, geofence_id, direction, min_s)
        to_drop = [key for key in self._dwell_satisfied_evaluated_since if key[1:] == suffix]
        for key in to_drop:
            del self._dwell_satisfied_evaluated_since[key]

    def _drop_device_bool_streak(self, key: tuple[DeviceFamilyId, str]) -> None:
        self._device_bool_since.pop(key, None)
        self._device_bool_value.pop(key, None)
        self._clear_device_dwell_satisfied_eval_for_device(key[0], key[1])

    def _drop_geofence_inside_since(self, key: tuple[str, str]) -> None:
        self._clear_dwell_satisfied_eval_for_geofence_direction(
            key[0],
            key[1],
            DwellDirection.INSIDE,
        )
        self._geofence_inside_since.pop(key, None)

    def _drop_geofence_outside_since(self, key: tuple[str, str]) -> None:
        self._clear_dwell_satisfied_eval_for_geofence_direction(
            key[0],
            key[1],
            DwellDirection.OUTSIDE,
        )
        self._geofence_outside_since.pop(key, None)

    def _set_device_bool_streak(
        self,
        key: tuple[DeviceFamilyId, str],
        value: bool,
        since: float,
    ) -> None:
        prior = self._device_bool_value.get(key)
        self._device_bool_value[key] = value
        self._device_bool_since[key] = since
        if prior is not None and prior != value:
            self._clear_device_dwell_satisfied_eval_for_device(key[0], key[1])

    def _set_geofence_inside_since(self, key: tuple[str, str], since: float) -> None:
        self._clear_dwell_satisfied_eval_for_geofence_direction(
            key[0],
            key[1],
            DwellDirection.INSIDE,
        )
        self._geofence_inside_since[key] = since

    def _set_geofence_outside_since(self, key: tuple[str, str], since: float) -> None:
        self._clear_dwell_satisfied_eval_for_geofence_direction(
            key[0],
            key[1],
            DwellDirection.OUTSIDE,
        )
        self._geofence_outside_since[key] = since

    def _setdefault_geofence_inside_since(
        self,
        key: tuple[str, str],
        since: float,
    ) -> float:
        existing = self._geofence_inside_since.get(key)
        if existing is not None:
            return existing
        self._set_geofence_inside_since(key, since)
        return since

    def _setdefault_geofence_outside_since(
        self,
        key: tuple[str, str],
        since: float,
    ) -> float:
        existing = self._geofence_outside_since.get(key)
        if existing is not None:
            return existing
        self._set_geofence_outside_since(key, since)
        return since

    def _sync_all_device_dwell_streaks(
        self,
        ctx: RuleEvaluationContext,
        now_epoch: float,
    ) -> None:
        device_state = ctx.device_state
        if device_state is None:
            return
        index = build_device_dwell_watch_index(list_automation_rules())
        seen: set[tuple[DeviceFamilyId, str]] = set()
        for watch in index.watches:
            backend_id = resolve_device_ref_to_backend_id(
                device_state,
                family_id=watch.family_id,
                device_ref=watch.device_id,
            )
            if backend_id is None:
                continue
            key = (watch.family_id, backend_id)
            if key in seen:
                continue
            seen.add(key)
            self._sync_device_bool_streak(
                watch.family_id,
                backend_id,
                now_epoch=now_epoch,
                ctx=ctx,
            )

    def _sync_device_bool_streak(
        self,
        family_id: DeviceFamilyId,
        backend_device_id: str,
        *,
        now_epoch: float,
        ctx: RuleEvaluationContext,
    ) -> None:
        key = (family_id, backend_device_id)
        current = natural_bool_for_device_family(
            ctx,
            family_id=family_id,
            device_id=backend_device_id,
        )
        if current is None:
            self._drop_device_bool_streak(key)
            return
        prior = self._device_bool_value.get(key)
        since = self._device_bool_since.get(key)
        if prior is None or since is None or prior != current:
            self._set_device_bool_streak(key, current, now_epoch)

    def _seed_dwell_satisfied_eval_debounce(self) -> None:
        """Warm debounce slots after restart for ongoing fired/consumed dwell episodes."""
        rules = list_automation_rules()
        index = build_dwell_watch_index(rules)
        now_epoch = self._now_fn()
        rules_by_id = {rule.id: rule for rule in rules}
        for watch in index.watches:
            roster_user_id = watch.rule_user_id
            since = self._dwell_streak_since(
                roster_user_id,
                watch.geofence_id,
                watch.direction,
            )
            if since is None or now_epoch - since < watch.min_s:
                continue
            presence_key = (roster_user_id, watch.geofence_id)
            episode = self._geofence_presence_episode.get(presence_key, 0)
            if watch.direction == DwellDirection.OUTSIDE:
                consumed = self._scheduled_outside_dwell_consumed
            else:
                consumed = self._scheduled_inside_dwell_consumed
            for rule_id in watch.rule_ids:
                rule = rules_by_id.get(rule_id)
                if rule is None:
                    continue
                runtime = self._rule_state.get(rule_id)
                fired_on_streak = (
                    runtime is not None and runtime.last_fired_at is not None and runtime.last_fired_at >= since
                )
                episode_consumed = consumed.get((rule_id, roster_user_id, watch.geofence_id)) == episode
                if not fired_on_streak and not episode_consumed:
                    continue
                debounce_key = (
                    rule_id,
                    roster_user_id,
                    watch.geofence_id,
                    watch.direction,
                    watch.min_s,
                )
                self._dwell_satisfied_evaluated_since[debounce_key] = since

    def _ensure_astronomical_schedule_materialized(
        self,
        rule: RuleOut,
        *,
        timezone: ZoneInfo,
        now: datetime,
        force: bool = False,
    ) -> str | None:
        if not uses_astronomical_materialized_schedule(rule):
            return None
        runtime = self._rule_state.setdefault(rule.id, _RuleRuntimeState())
        local_date = local_calendar_date(now.timestamp(), timezone)
        if (
            not force
            and runtime.schedule_materialized_for == local_date
            and runtime.effective_schedule_cron is not None
            and runtime.next_evaluate_at is not None
        ):
            return runtime.effective_schedule_cron
        settings = load_settings_location()
        sun = compute_rules_sun_out(settings, now=now)
        cron_expr = materialize_astronomical_cron(
            rule,
            sun=sun,
            timezone=timezone,
        )
        if cron_expr is None:
            return None
        runtime.effective_schedule_cron = cron_expr
        runtime.schedule_materialized_for = local_date
        if uses_astronomical_repeat_schedule(rule):
            runtime.next_evaluate_at = next_astronomical_repeat_evaluate_at(
                rule,
                settings=settings,
                timezone=timezone,
                now=now,
                due_if_inside_window=True,
            )
        else:
            runtime.next_evaluate_at = next_scheduled_evaluate_at(
                cron_expr,
                now,
                timezone,
                due_if_matching=True,
            )
        self._persist_rule_schedule_state(rule.id)
        return cron_expr

    def _refresh_astronomical_schedules_for_new_day(self) -> None:
        settings = load_settings_location()
        timezone = ZoneInfo(settings.timezone)
        now = datetime.fromtimestamp(self._now_fn(), tz=timezone)
        local_date = local_calendar_date(now.timestamp(), timezone)
        if self._last_astronomical_materialization_date == local_date:
            return
        self._last_astronomical_materialization_date = local_date
        for rule in list_automation_rules():
            if not rule.enabled or not uses_astronomical_materialized_schedule(rule):
                continue
            runtime = self._rule_state.get(rule.id)
            if (
                runtime is not None
                and runtime.schedule_materialized_for == local_date
                and runtime.effective_schedule_cron is not None
                and runtime.next_evaluate_at is not None
            ):
                continue
            self._ensure_astronomical_schedule_materialized(
                rule,
                timezone=timezone,
                now=now,
                force=True,
            )

    def _seed_scheduled_dwell_consumed_from_persisted_fire(self) -> None:
        """Reconstruct dwell episode consumption after restart for ongoing streaks."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        roster_user_ids = {row.user_id for row in list_users(cache_path)}
        for rule in list_automation_rules():
            if not _rule_has_dwell_condition(rule):
                continue
            if (
                RuleTrigger.DEVICE_STATE not in rule.triggers
                and RuleTrigger.DWELL_SATISFIED not in rule.triggers
                and RuleTrigger.SCHEDULED not in rule.triggers
            ):
                continue
            runtime = self._rule_state.get(rule.id)
            if runtime is None or runtime.last_fired_at is None:
                continue
            for condition in iter_dwell_for_s_conditions(rule.conditions.all):
                if isinstance(condition, UsersOutsideGeofenceForSCondition):
                    consumed = self._scheduled_outside_dwell_consumed
                    streak_by_key = self._geofence_outside_since
                else:
                    consumed = self._scheduled_inside_dwell_consumed
                    streak_by_key = self._geofence_inside_since
                for rule_user_id in condition.user_ids:
                    roster_user_id = rule_user_id.strip()
                    if roster_user_id not in roster_user_ids:
                        continue
                    key = (roster_user_id, condition.geofence_id)
                    streak_start = streak_by_key.get(key)
                    if streak_start is None:
                        continue
                    if runtime.last_fired_at >= streak_start:
                        episode = self._geofence_presence_episode.get(key, 0)
                        consumed[(rule.id, roster_user_id, condition.geofence_id)] = episode

    def _skip_if_daily_cap(
        self,
        *,
        log_user_ids: str,
        now_epoch: float,
        rule: RuleOut,
        runtime: _RuleRuntimeState,
        timezone: ZoneInfo,
    ) -> bool:
        if not rule.fire_once_per_local_day:
            return False
        if not fired_on_same_local_calendar_day(
            runtime.last_fired_at,
            now_epoch,
            timezone,
        ):
            return False
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
        return True

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
            key: value for key, value in self._geofence_was_inside.items() if key[0] == user_id
        }
        if not mutate_state and _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug(
                "[rules] geofence edge state unchanged user_id=%s accuracy_m=%s limit_m=%s",
                user_id,
                location.accuracy_m,
                accuracy_limit_m,
            )
        if mutate_state and self._cache_path is not None:
            rules = list_automation_rules()
            retention = load_location_history_retention(self._cache_path)
            history_since = _geofence_seed_history_since_epoch(
                rules,
                now=location.reported_at,
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
            observed_at=location.reported_at,
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
        home_wifi_bssid = _home_wifi_bssid_for_user(self._cache_path, user_id)
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
                    home_wifi_bssid=home_wifi_bssid,
                    observed_wifi_bssid=location.wifi_bssid,
                )
            now_inside_for_dwell = gps_inside or wifi_dwell_inside
            track_dwell = dwell_accuracy_limit_m is not None and (
                _location_accuracy_passes(location, dwell_accuracy_limit_m) or wifi_dwell_inside
            )
            transition = GeofenceTransition()
            episode_bumped = False
            if gps_inside:
                self._geofence_geo_outside_streak_since.pop(key, None)
                streak_since = self._geofence_geo_inside_streak_since.get(key)
                if streak_since is None:
                    streak_since = observed_at
                    self._geofence_geo_inside_streak_since[key] = streak_since
                if not was_inside and observed_at - streak_since >= _GEO_INSIDE_STATE_RECONCILE_S:
                    outside_since = self._geofence_outside_since.get(key)
                    dwell_elapsed = outside_since is None or (
                        observed_at - outside_since >= _MIN_GEOFENCE_OUTSIDE_DWELL_S
                    )
                    if mutate_state:
                        if not was_inside:
                            self._bump_geofence_presence_episode(user_id, geofence_id)
                        self._geofence_was_inside[key] = True
                        self._drop_geofence_outside_since(key)
                        was_inside = True
                        if track_dwell:
                            self._setdefault_geofence_inside_since(key, streak_since)
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
                                dwell_remaining_s=_MIN_GEOFENCE_OUTSIDE_DWELL_S - (observed_at - outside_since),
                            )
            else:
                self._geofence_geo_inside_streak_since.pop(key, None)
                outside_streak_since = self._geofence_geo_outside_streak_since.get(key)
                if outside_streak_since is None:
                    outside_streak_since = observed_at
                    self._geofence_geo_outside_streak_since[key] = outside_streak_since
                if (
                    was_inside or prior_was_inside
                ) and observed_at - outside_streak_since >= _GEO_OUTSIDE_STATE_RECONCILE_S:
                    if mutate_state:
                        if was_inside or prior_was_inside:
                            self._bump_geofence_presence_episode(user_id, geofence_id)
                            episode_bumped = True
                        self._geofence_was_inside[key] = False
                        was_inside = False
                        if track_dwell:
                            self._drop_geofence_inside_since(key)
                        self._set_geofence_outside_since(key, outside_streak_since)
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
                dwell_elapsed = outside_since is None or (observed_at - outside_since >= _MIN_GEOFENCE_OUTSIDE_DWELL_S)
                if mutate_state:
                    if not was_inside:
                        self._bump_geofence_presence_episode(user_id, geofence_id)
                    self._geofence_was_inside[key] = True
                    self._drop_geofence_outside_since(key)
                    if track_dwell:
                        self._set_geofence_inside_since(key, observed_at)
                    if dwell_elapsed:
                        transition = GeofenceTransition(entered=True)
                    elif outside_since is not None:
                        _log_geofence_enter_debounced(
                            user_id=user_id,
                            geofence_id=geofence_id,
                            outside_s=observed_at - outside_since,
                            dwell_remaining_s=_MIN_GEOFENCE_OUTSIDE_DWELL_S - (observed_at - outside_since),
                        )
            elif (was_inside or depart_edge_pending) and not now_inside_for_dwell:
                leaving_from_inside = was_inside or depart_edge_pending
                if mutate_state:
                    if leaving_from_inside and not episode_bumped:
                        self._bump_geofence_presence_episode(user_id, geofence_id)
                    self._geofence_was_inside[key] = False
                    if track_dwell:
                        self._drop_geofence_inside_since(key)
                    transition = GeofenceTransition(left=True)
                if track_dwell:
                    if leaving_from_inside:
                        self._set_geofence_outside_since(key, observed_at)
                    else:
                        self._setdefault_geofence_outside_since(key, observed_at)
                    self._drop_geofence_inside_since(key)
            elif now_inside_for_dwell:
                was_outside = not self._geofence_was_inside.get(key, False)
                if mutate_state or wifi_dwell_inside:
                    if was_outside:
                        self._bump_geofence_presence_episode(user_id, geofence_id)
                    self._geofence_was_inside[key] = True
                    self._drop_geofence_outside_since(key)
                if track_dwell and key not in self._geofence_inside_since:
                    self._set_geofence_inside_since(key, observed_at)
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
                    self._setdefault_geofence_outside_since(key, observed_at)
                    self._drop_geofence_inside_since(key)
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
        DevicesAnyInStateForSCondition | UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition,
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
        return any(_condition_triggered_geofence_edge(child, user_id, transitions) for child in condition.conditions)
    if isinstance(condition, AnyConditionsCondition):
        if not condition.conditions:
            return False
        return any(_condition_triggered_geofence_edge(child, user_id, transitions) for child in condition.conditions)
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
    *,
    fire_source: RuleFireSource,
) -> str:
    parts: list[str] = []
    for row in evaluation.conditions:
        if (
            fire_source not in ("device_state", "dwell_satisfied", "eligibility", "scheduled")
            and RuleTrigger.EDGE_TRUE in rule.triggers
            and isinstance(
                row.condition,
                (UsersInsideGeofenceCondition, UsersOutsideGeofenceCondition),
            )
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
    if (
        isinstance(
            condition,
            (DevicesAllInStateCondition, DevicesAnyInStateCondition),
        )
        and condition.state == DeviceConditionState.OPEN
    ):
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
    unmet = [f"{row.label} ({row.detail})" for row in evaluation.conditions if not row.met]
    return "; ".join(unmet) if unmet else "none"


def _geofence_dwell_accuracy_limit_m(rules: list[RuleOut]) -> int | None:
    """Return the strictest accuracy limit among enabled rules with dwell conditions."""
    limits = [rule.min_location_accuracy_m for rule in rules if rule.enabled and _rule_has_dwell_condition(rule)]
    if not limits:
        return None
    return min(limits)


def _geofence_edge_accuracy_limit_m(rules: list[RuleOut]) -> int | None:
    """Return the strictest accuracy limit among enabled ``edge_true`` rules."""
    limits = [rule.min_location_accuracy_m for rule in rules if rule.enabled and RuleTrigger.EDGE_TRUE in rule.triggers]
    if not limits:
        return None
    return min(limits)


def _geofence_seed_history_since_epoch(
    rules: list[RuleOut],
    *,
    now: float,
    retention: LocationHistoryRetention,
) -> float:
    """Return the oldest ``reported_at`` to load when seeding geofence streak state."""
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


def _home_wifi_bssid_for_user(
    cache_path: Path | None,
    user_id: str,
) -> str | None:
    if cache_path is None:
        return None
    for row in list_users(cache_path):
        if row.user_id == user_id:
            return row.home_wifi_bssid
    return None


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


def _location_fix_at_iso(location: UserLocationRecord) -> str:
    """Format ``location.fix_at`` as a UTC ISO-8601 string with a ``Z`` suffix."""
    return location_epoch_to_iso_z(location.fix_at)


def _location_reported_at_iso(location: UserLocationRecord) -> str:
    """Format ``location.reported_at`` as a UTC ISO-8601 string with a ``Z`` suffix."""
    return location_epoch_to_iso_z(location.reported_at)


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
    if record.last_location_received_at < location.reported_at:
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
    home_wifi_bssid: str | None = None,
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
            home_wifi_bssid=home_wifi_bssid,
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
            streak_start = row.reported_at
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
                home_wifi_bssid=home_wifi_bssid,
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
                streak_start = row.reported_at
                if wifi_home_presence_applies(
                    settings,
                    geofence.geofence_id,
                    row.connection_type,
                    accuracy_m=row.accuracy_m,
                    geofences=geofences,
                    lat=row.lat,
                    lon=row.lon,
                    min_accuracy_m=dwell_accuracy_limit_m,
                    home_wifi_bssid=home_wifi_bssid,
                    observed_wifi_bssid=row.wifi_bssid,
                ):
                    streak_wifi_row = row
            inside_since = streak_start
            if user_id is not None and streak_wifi_row is not None and inside_since == streak_wifi_row.reported_at:
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
        "[rules] geofence enter suppressed user_id=%s geofence_id=%s outside_s=%.0f dwell_remaining_s=%.0f",
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


def _log_device_state_evaluation_task(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.exception(
            "[rules] device-state evaluation task failed",
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


def _rule_uses_scheduled_evaluation_tick(rule: RuleOut) -> bool:
    return RuleTrigger.SCHEDULED in rule.triggers or uses_astronomical_eligibility_wake(rule)


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
    return any(_condition_triggered_geofence_edge(condition, user_id, transitions) for condition in conditions)
