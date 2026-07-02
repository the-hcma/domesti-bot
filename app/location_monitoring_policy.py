"""Proactive location monitoring: stale watchdog and geofence approach mode."""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.api.schemas import RuleOut
from app.automation_rules_loader import list_automation_rules
from app.device_enums import RuleTrigger
from app.location_request_coordinator import (
    DeferredAccuracyEdgeSnapshot,
    LocationRequestCoordinator,
    LocationRequestContext,
)
from app.mytracks_logging import mytracks_logger
from app.mytracks_store import (
    load_approach_monitoring_distance_m,
    load_location_request_rate_limits,
)
from app.presence_store import UserLocationRecord, _haversine_m, list_user_locations
from app.rule_validation import collect_rule_geofence_ids, collect_rule_user_ids
from app.rules_store import GeofenceRecord, list_geofences

_LOGGER = mytracks_logger(__name__)
_APPROACH_MONITOR_TICK_S = 1.0


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no"}


def _env_float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.environ.get(name)
    try:
        value = default if raw is None else float(raw)
    except ValueError:
        _LOGGER.warning("Invalid %s=%r; using default %.1f", name, raw, default)
        return default
    if minimum is not None and value < minimum:
        _LOGGER.warning("Invalid %s=%r; using default %.1f", name, raw, default)
        return default
    return value


_APPROACH_ENABLED = _env_bool("DOMESTI_LOCATION_APPROACH_ENABLED", True)
_APPROACH_REQUEST_INTERVAL_S = _env_float(
    "DOMESTI_LOCATION_APPROACH_INTERVAL_S",
    5.0,
    minimum=0.0,
)
_STALE_INTERVAL_S = _env_float(
    "DOMESTI_LOCATION_STALE_INTERVAL_S",
    1800.0,
    minimum=1.0,
)
ApproachExitReason = Literal["beyond_corridor", "inside_accurate", "no_rules"]


@dataclass
class _ApproachModeState:
    entered_at: float
    geofence_id: str
    last_request_at: float | None
    rule_id: str | None
    user_id: str


class LocationMonitoringPolicy:
    """Background stale watchdog and geofence approach monitoring."""

    def __init__(
        self,
        *,
        cache_path: Path | None,
        coordinator: LocationRequestCoordinator,
        deferred_edges_for_user: Callable[
            [str],
            tuple[DeferredAccuracyEdgeSnapshot, ...],
        ],
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._coordinator = coordinator
        self._deferred_edges_for_user = deferred_edges_for_user
        self._now_fn = now_fn or time.time
        self._approach_by_key: dict[tuple[str, str], _ApproachModeState] = {}
        self._stop = asyncio.Event()
        self._approach_task: asyncio.Task[None] | None = None
        self._stale_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        self._stop.set()
        for task in (self._approach_task, self._stale_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._approach_task = None
        self._stale_task = None

    def on_location_updated(
        self,
        user_id: str,
        *,
        location: UserLocationRecord,
        now: float,
    ) -> None:
        """Re-evaluate approach corridors after a location ingest."""
        if not _APPROACH_ENABLED:
            return
        cache_path = self._cache_path
        if cache_path is None:
            return
        approach_distance_m = load_approach_monitoring_distance_m(cache_path)
        geofence_by_id = {row.geofence_id: row for row in list_geofences(cache_path)}
        edge_rules = _enabled_edge_rules_for_user(user_id)
        active_geofence_ids = _active_geofence_ids_for_edge_rules(edge_rules)
        for geofence_id in sorted(active_geofence_ids):
            geofence = geofence_by_id.get(geofence_id)
            if geofence is None or not geofence.enabled:
                self._exit_approach(user_id, geofence_id, reason="no_rules")
                continue
            min_accuracy = _min_accuracy_for_geofence(edge_rules, geofence_id)
            if _should_exit_approach_inside(
                location,
                geofence,
                min_accuracy_m=min_accuracy,
            ):
                self._exit_approach(user_id, geofence_id, reason="inside_accurate")
                continue
            if not _is_in_approach_corridor(
                location,
                geofence,
                approach_distance_m=approach_distance_m,
            ):
                self._exit_approach(user_id, geofence_id, reason="beyond_corridor")
                continue
            self._enter_approach(
                user_id,
                geofence_id=geofence_id,
                now=now,
                rule_id=_first_edge_rule_for_geofence(edge_rules, geofence_id),
            )

    def record_approach_request_throttled(
        self,
        user_id: str,
        reason: str,
        now: float,
        geofence_id: str | None,
    ) -> None:
        """Stamp approach throttle after my-tracks accepts or returns cooldown."""
        if reason != "approach_monitoring" or geofence_id is None:
            return
        state = self._approach_by_key.get((user_id, geofence_id))
        if state is not None:
            state.last_request_at = now

    def start_background_loops(self) -> None:
        if self._stale_task is not None or self._approach_task is not None:
            return
        self._stop.clear()
        self._stale_task = asyncio.create_task(
            self._stale_watchdog_loop(),
            name="location-stale-watchdog",
        )
        if _APPROACH_ENABLED:
            self._seed_approach_from_stored_locations()
            self._approach_task = asyncio.create_task(
                self._approach_monitor_loop(),
                name="location-approach-monitor",
            )

    async def _approach_monitor_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_APPROACH_MONITOR_TICK_S)
                break
            except TimeoutError:
                pass
            try:
                await self._run_approach_monitor_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("location monitoring approach tick failed")

    async def _approach_monitor_tick_for_user(
        self,
        state: _ApproachModeState,
        *,
        location: UserLocationRecord | None,
        now: float,
        approach_distance_m: int,
        geofence_by_id: dict[str, GeofenceRecord],
    ) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        path = cache_path
        geofence = geofence_by_id.get(state.geofence_id)
        if geofence is None or not geofence.enabled:
            self._exit_approach(state.user_id, state.geofence_id, reason="no_rules")
            return
        edge_rules = _enabled_edge_rules_for_user(state.user_id)
        if state.geofence_id not in _active_geofence_ids_for_edge_rules(edge_rules):
            self._exit_approach(state.user_id, state.geofence_id, reason="no_rules")
            return
        if location is None:
            return
        min_accuracy = _min_accuracy_for_geofence(edge_rules, state.geofence_id)
        if _should_exit_approach_inside(
            location,
            geofence,
            min_accuracy_m=min_accuracy,
        ):
            self._exit_approach(state.user_id, state.geofence_id, reason="inside_accurate")
            return
        if not _is_in_approach_corridor(
            location,
            geofence,
            approach_distance_m=approach_distance_m,
        ):
            self._exit_approach(state.user_id, state.geofence_id, reason="beyond_corridor")
            return
        interval_s = _effective_approach_request_interval_s(path)
        throttle_anchor_at = (
            state.last_request_at if state.last_request_at is not None else state.entered_at
        )
        if (now - throttle_anchor_at) < interval_s:
            return
        context = LocationRequestContext(
            deferred_edges=self._deferred_edges_for_user(state.user_id),
            location=location,
            now=now,
        )
        self._coordinator.schedule_request_with_reason(
            state.user_id,
            reason="approach_monitoring",
            context=context,
            geofence_id=state.geofence_id,
            require_edge_rules=True,
            rule_id=state.rule_id,
        )

    async def _run_approach_monitor_tick(self) -> None:
        cache_path = self._cache_path
        if cache_path is None or not self._approach_by_key:
            return
        now = self._now_fn()
        approach_distance_m = load_approach_monitoring_distance_m(cache_path)
        locations = list_user_locations(cache_path)
        geofence_by_id = {row.geofence_id: row for row in list_geofences(cache_path)}
        for state in list(self._approach_by_key.values()):
            await self._approach_monitor_tick_for_user(
                state,
                location=locations.get(state.user_id),
                now=now,
                approach_distance_m=approach_distance_m,
                geofence_by_id=geofence_by_id,
            )

    async def _run_stale_watchdog_tick(self) -> None:
        cache_path = self._cache_path
        if cache_path is None or _STALE_INTERVAL_S <= 0:
            return
        now = self._now_fn()
        locations = list_user_locations(cache_path)
        for user_id in sorted(monitored_user_ids()):
            location = locations.get(user_id)
            if location is not None and (now - location.reported_at) <= _STALE_INTERVAL_S:
                continue
            context = LocationRequestContext(
                deferred_edges=self._deferred_edges_for_user(user_id),
                location=location or _synthetic_stale_location(user_id, now),
                now=now,
            )
            self._coordinator.schedule_request_with_reason(
                user_id,
                reason="stale_watchdog",
                context=context,
                geofence_id=None,
                require_edge_rules=False,
                rule_id=None,
            )

    def _seed_approach_from_stored_locations(self) -> None:
        """Seed approach mode from cached locations when loops start (e.g. after restart)."""
        if not _APPROACH_ENABLED:
            return
        cache_path = self._cache_path
        if cache_path is None:
            return
        now = self._now_fn()
        locations = list_user_locations(cache_path)
        for user_id, location in sorted(locations.items()):
            if not _enabled_edge_rules_for_user(user_id):
                continue
            self.on_location_updated(user_id, location=location, now=now)

    async def _stale_watchdog_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=_STALE_INTERVAL_S)
                break
            except TimeoutError:
                pass
            try:
                await self._run_stale_watchdog_tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("location monitoring stale watchdog tick failed")

    def _enter_approach(
        self,
        user_id: str,
        *,
        geofence_id: str,
        now: float,
        rule_id: str | None,
    ) -> None:
        key = (user_id, geofence_id)
        existing = self._approach_by_key.get(key)
        if existing is not None:
            existing.rule_id = rule_id
            return
        self._approach_by_key[key] = _ApproachModeState(
            entered_at=now,
            geofence_id=geofence_id,
            last_request_at=None,
            rule_id=rule_id,
            user_id=user_id,
        )
        _LOGGER.info(
            "location monitoring approach_mode entered user=%s geofence_id=%s",
            user_id,
            geofence_id,
        )

    def _exit_approach(
        self,
        user_id: str,
        geofence_id: str,
        *,
        reason: ApproachExitReason,
    ) -> None:
        key = (user_id, geofence_id)
        if key not in self._approach_by_key:
            return
        del self._approach_by_key[key]
        _LOGGER.info(
            "location monitoring approach_mode exited user=%s geofence_id=%s reason=%s",
            user_id,
            geofence_id,
            reason,
        )


def approach_request_interval_s() -> float:
    return _APPROACH_REQUEST_INTERVAL_S


def monitored_user_ids() -> set[str]:
    user_ids: set[str] = set()
    for rule in list_automation_rules():
        if not rule.enabled:
            continue
        user_ids.update(collect_rule_user_ids(rule))
    return user_ids


def _active_geofence_ids_for_edge_rules(edge_rules: list[RuleOut]) -> set[str]:
    geofence_ids: set[str] = set()
    for rule in edge_rules:
        geofence_ids.update(collect_rule_geofence_ids(rule))
    return geofence_ids


def _distance_to_edge_m(location: UserLocationRecord, geofence: GeofenceRecord) -> float:
    distance_to_center = _haversine_m(
        location.lat,
        location.lon,
        geofence.center_lat,
        geofence.center_lon,
    )
    return distance_to_center - float(geofence.radius_m)


def _effective_approach_request_interval_s(path: Path) -> float:
    limits = load_location_request_rate_limits(path)
    configured = _APPROACH_REQUEST_INTERVAL_S
    if limits is None:
        return configured
    cooldown = float(limits.effective_user_cooldown_seconds("approach_monitoring"))
    return max(configured, cooldown)


def _enabled_edge_rules_for_user(user_id: str) -> list[RuleOut]:
    return [
        rule
        for rule in list_automation_rules()
        if rule.enabled
        and RuleTrigger.EDGE_TRUE in rule.triggers
        and user_id in collect_rule_user_ids(rule)
    ]


def _first_edge_rule_for_geofence(
    edge_rules: list[RuleOut],
    geofence_id: str,
) -> str | None:
    for rule in edge_rules:
        if geofence_id in collect_rule_geofence_ids(rule):
            return rule.id
    return None


def _is_in_approach_corridor(
    location: UserLocationRecord,
    geofence: GeofenceRecord,
    *,
    approach_distance_m: int,
) -> bool:
    distance_to_edge_m = _distance_to_edge_m(location, geofence)
    if distance_to_edge_m <= 0:
        return False
    return distance_to_edge_m <= float(approach_distance_m)


def _min_accuracy_for_geofence(edge_rules: list[RuleOut], geofence_id: str) -> int | None:
    minimum: int | None = None
    for rule in edge_rules:
        if geofence_id not in collect_rule_geofence_ids(rule):
            continue
        current = rule.min_location_accuracy_m
        if minimum is None or current < minimum:
            minimum = current
    return minimum


def _should_exit_approach_inside(
    location: UserLocationRecord,
    geofence: GeofenceRecord,
    *,
    min_accuracy_m: int | None,
) -> bool:
    distance_to_edge_m = _distance_to_edge_m(location, geofence)
    if distance_to_edge_m > 0:
        return False
    return _location_accuracy_passes(location, min_accuracy_m)


def _location_accuracy_passes(
    location: UserLocationRecord,
    limit_m: int | None,
) -> bool:
    if limit_m is None:
        return True
    if location.accuracy_m is None:
        return True
    return location.accuracy_m <= limit_m


def _synthetic_stale_location(user_id: str, now: float) -> UserLocationRecord:
    return UserLocationRecord(
        accuracy_m=None,
        battery_level=None,
        connection_type=None,
        fix_source=None,
        fix_at=0.0,
        lat=0.0,
        lon=0.0,
        reported_at=0.0,
        source="stale_watchdog",
        trigger=None,
        user_id=user_id,
        wifi_bssid=None,
        wifi_ssid=None,
    )
