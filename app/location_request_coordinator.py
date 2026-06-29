"""Decide when domesti-bot should ask my-tracks for a fresher OwnTracks location update."""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.api.schemas import RuleOut
from app.automation_rules_loader import list_automation_rules, load_settings_location
from app.db.secrets import SecretsDecryptError, load_mytracks_relay_api_key_from_db
from app.mytracks_logging import mytracks_log_host, mytracks_logger
from app.mytracks_service import request_user_location
from app.mytracks_store import (
    load_mytracks_pair_status,
    load_remote_request_location_enabled,
    set_remote_request_location_enabled,
)
from app.presence_connection_type import connection_type_is_wifi
from app.presence_store import (
    UserLocationRecord,
    _haversine_m,
    list_user_location_history_for_user,
)
from app.presence_wifi import normalize_wifi_bssid, wifi_bssids_match
from app.rule_validation import collect_rule_geofence_ids, collect_rule_user_ids
from app.rules_store import GeofenceRecord, list_geofences, list_users
from app.wifi_home_presence import wifi_home_geofence_ids

_LOGGER = mytracks_logger(__name__)
_LOCATION_REQUEST_REASON = Literal[
    "accuracy_streak",
    "boundary_proximity",
    "deferred_edge",
]
DeferredGeofenceEvent = Literal["entered", "left"]

ACCURACY_STREAK_COUNT = int(os.environ.get("DOMESTI_LOCATION_REQUEST_ACCURACY_STREAK", "3"))
BOUNDARY_MARGIN_M = float(os.environ.get("DOMESTI_LOCATION_REQUEST_BOUNDARY_MARGIN_M", "25"))
DEFERRED_EDGE_FRACTION = float(
    os.environ.get("DOMESTI_LOCATION_REQUEST_DEFERRED_EDGE_FRACTION", "0.5"),
)
# Matches my-tracks LOCATION_REQUEST_USER_COOLDOWN_SECONDS (30 s) when the server
# omits cooldown_until on 202/409.
LOCATION_REQUEST_COOLDOWN_S = float(
    os.environ.get("DOMESTI_LOCATION_REQUEST_COOLDOWN_S", "30"),
)


@dataclass(frozen=True)
class DeferredAccuracyEdgeSnapshot:
    event: DeferredGeofenceEvent
    expires_at: float
    geofence_id: str
    observed_at: float
    rule_id: str
    user_id: str


@dataclass(frozen=True)
class LocationRequestContext:
    deferred_edges: tuple[DeferredAccuracyEdgeSnapshot, ...]
    location: UserLocationRecord
    now: float


class LocationRequestCoordinator:
    """Fire-and-forget my-tracks reportLocation requests for geofence accuracy."""

    def __init__(
        self,
        *,
        cache_path: Path | None,
        now_fn: Callable[[], float] | None = None,
    ) -> None:
        self._cache_path = cache_path
        self._cooldown_until_by_user: dict[str, float] = {}
        self._in_flight_user_ids: set[str] = set()
        self._now_fn = now_fn or time.time

    def _accuracy_streak_trigger(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
        edge_rules: list[RuleOut],
    ) -> tuple[str | None, str | None, _LOCATION_REQUEST_REASON] | None:
        cache_path = self._cache_path
        if cache_path is None or ACCURACY_STREAK_COUNT <= 0:
            return None
        history = list_user_location_history_for_user(cache_path, user_id)
        recent = history[-ACCURACY_STREAK_COUNT:]
        if len(recent) < ACCURACY_STREAK_COUNT:
            return None
        for rule in edge_rules:
            if all(
                not _location_accuracy_passes(row, rule.min_location_accuracy_m)
                for row in recent
            ):
                return rule.id, None, "accuracy_streak"
        return None

    def _boundary_proximity_trigger(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
        edge_rules: list[RuleOut],
        geofence_by_id: dict[str, GeofenceRecord],
    ) -> tuple[str | None, str | None, _LOCATION_REQUEST_REASON] | None:
        location = context.location
        accuracy_m = location.accuracy_m
        if accuracy_m is None:
            return None
        active_geofence_ids: set[str] = set()
        min_accuracy_by_geofence: dict[str, int] = {}
        for rule in edge_rules:
            for geofence_id in collect_rule_geofence_ids(rule):
                active_geofence_ids.add(geofence_id)
                current = min_accuracy_by_geofence.get(geofence_id)
                if current is None or rule.min_location_accuracy_m < current:
                    min_accuracy_by_geofence[geofence_id] = rule.min_location_accuracy_m
        for geofence_id in sorted(active_geofence_ids):
            geofence = geofence_by_id.get(geofence_id)
            if geofence is None or not geofence.enabled:
                continue
            min_accuracy = min_accuracy_by_geofence.get(geofence_id)
            if min_accuracy is None:
                continue
            if _location_accuracy_passes(location, min_accuracy):
                continue
            distance_to_center = _haversine_m(
                location.lat,
                location.lon,
                geofence.center_lat,
                geofence.center_lon,
            )
            distance_to_edge_m = distance_to_center - float(geofence.radius_m)
            threshold_m = max(float(accuracy_m), float(min_accuracy)) + BOUNDARY_MARGIN_M
            if abs(distance_to_edge_m) > threshold_m:
                continue
            rule_id = _first_edge_rule_for_geofence(edge_rules, geofence_id)
            return rule_id, geofence_id, "boundary_proximity"
        return None

    def _deferred_edge_trigger(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
        edge_rules: list[RuleOut],
    ) -> tuple[str | None, str | None, _LOCATION_REQUEST_REASON] | None:
        if not context.deferred_edges:
            return None
        rules_by_id = {rule.id: rule for rule in edge_rules}
        for deferred in context.deferred_edges:
            rule = rules_by_id.get(deferred.rule_id)
            if rule is None:
                continue
            grace_s = rule.accuracy_edge_grace_s
            if grace_s is None or grace_s <= 0:
                continue
            if _location_accuracy_passes(context.location, rule.min_location_accuracy_m):
                continue
            registered_at = deferred.expires_at - float(grace_s)
            elapsed_fraction = (context.now - registered_at) / float(grace_s)
            if elapsed_fraction < DEFERRED_EDGE_FRACTION:
                continue
            return deferred.rule_id, deferred.geofence_id, "deferred_edge"
        return None

    async def _maybe_request_async(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
    ) -> None:
        cache_path = self._cache_path
        if cache_path is None:
            return
        trimmed = user_id.strip()
        if trimmed == "":
            return
        if trimmed in self._in_flight_user_ids:
            return
        self._in_flight_user_ids.add(trimmed)
        try:
            edge_rules = _enabled_edge_rules_for_user(trimmed)
            if not edge_rules:
                return
            if _user_confidently_inside_via_home_wifi(
                cache_path,
                trimmed,
                context.location,
            ):
                _LOGGER.debug(
                    "location request skipped user=%s reason=wifi_home_bssid",
                    trimmed,
                )
                return
            remote_enabled = load_remote_request_location_enabled(cache_path)
            if remote_enabled is not True:
                _LOGGER.debug(
                    "location request skipped user=%s reason=remote_request_disabled",
                    trimmed,
                )
                return
            if not load_mytracks_pair_status(cache_path):
                return
            if self._user_in_local_cooldown(trimmed, now=context.now):
                _LOGGER.debug(
                    "location request skipped user=%s reason=local_cooldown",
                    trimmed,
                )
                return
            trigger = self._select_trigger(
                trimmed,
                context=context,
                edge_rules=edge_rules,
            )
            if trigger is None:
                return
            rule_id, geofence_id, reason = trigger
            try:
                relay_key = load_mytracks_relay_api_key_from_db(cache_path)
            except SecretsDecryptError as exc:
                _LOGGER.warning(
                    "location request skipped user=%s reason=relay_key_decrypt_failed detail=%s",
                    trimmed,
                    exc,
                )
                return
            if relay_key is None or relay_key.strip() == "":
                _LOGGER.debug(
                    "location request skipped user=%s reason=relay_key_missing",
                    trimmed,
                )
                return
            pair_status = load_mytracks_pair_status(cache_path)
            if pair_status is None or pair_status.paired_at is None:
                return
            result = await request_user_location(
                base_url=pair_status.domain,
                relay_api_key=relay_key,
                user_id=trimmed,
                reason=reason,
                rule_id=rule_id,
                geofence_id=geofence_id,
            )
            if result.status == "accepted":
                self._cooldown_until_by_user[trimmed] = _cooldown_until_from_result(
                    now=context.now,
                    cooldown_until_epoch=result.cooldown_until_epoch,
                )
                _LOGGER.info(
                    "location request accepted user=%s reason=%s rule_id=%s geofence_id=%s host=%s",
                    trimmed,
                    reason,
                    rule_id or "",
                    geofence_id or "",
                    mytracks_log_host(pair_status.domain),
                )
                return
            if result.status == "cooldown":
                self._cooldown_until_by_user[trimmed] = _cooldown_until_from_result(
                    now=context.now,
                    cooldown_until_epoch=result.cooldown_until_epoch,
                )
                _LOGGER.debug(
                    "location request cooldown user=%s reason=%s until=%s",
                    trimmed,
                    reason,
                    result.cooldown_until_iso or "",
                )
                return
            if result.status == "disabled":
                set_remote_request_location_enabled(cache_path, enabled=False)
                _LOGGER.info(
                    "location request disabled on my-tracks host=%s; skipping future requests",
                    mytracks_log_host(pair_status.domain),
                )
                return
            _LOGGER.warning(
                "location request failed user=%s reason=%s detail=%s",
                trimmed,
                reason,
                result.detail or "",
            )
        finally:
            self._in_flight_user_ids.discard(trimmed)

    def _select_trigger(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
        edge_rules: list[RuleOut],
    ) -> tuple[str | None, str | None, _LOCATION_REQUEST_REASON] | None:
        cache_path = self._cache_path
        if cache_path is None:
            return None
        geofences = list_geofences(cache_path)
        geofence_by_id = {row.geofence_id: row for row in geofences}
        deferred = self._deferred_edge_trigger(
            user_id,
            context=context,
            edge_rules=edge_rules,
        )
        if deferred is not None:
            return deferred
        streak = self._accuracy_streak_trigger(
            user_id,
            context=context,
            edge_rules=edge_rules,
        )
        if streak is not None:
            return streak
        return self._boundary_proximity_trigger(
            user_id,
            context=context,
            edge_rules=edge_rules,
            geofence_by_id=geofence_by_id,
        )

    def _user_in_local_cooldown(self, user_id: str, *, now: float) -> bool:
        cooldown_until = self._cooldown_until_by_user.get(user_id)
        if cooldown_until is None:
            return False
        if now >= cooldown_until:
            self._cooldown_until_by_user.pop(user_id, None)
            return False
        return True

    def maybe_request(
        self,
        user_id: str,
        *,
        context: LocationRequestContext,
    ) -> None:
        """Schedule an async location request when triggers match; never blocks callers."""
        cache_path = self._cache_path
        if cache_path is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(
            self._maybe_request_async(user_id, context=context),
            name=f"location-request-{user_id}",
        )
        task.add_done_callback(_log_location_request_task)


def _cooldown_until_from_result(
    *,
    now: float,
    cooldown_until_epoch: float | None,
) -> float:
    if cooldown_until_epoch is not None:
        return cooldown_until_epoch
    return now + LOCATION_REQUEST_COOLDOWN_S


def _enabled_edge_rules_for_user(user_id: str) -> list[RuleOut]:
    return [
        rule
        for rule in list_automation_rules()
        if rule.enabled
        and rule.trigger == "edge_true"
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


def _location_accuracy_passes(
    location: UserLocationRecord,
    limit_m: int | None,
) -> bool:
    if limit_m is None:
        return True
    if location.accuracy_m is None:
        return True
    return location.accuracy_m <= limit_m


def _log_location_request_task(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _LOGGER.exception("location request task failed", exc_info=exc)


def _user_confidently_inside_via_home_wifi(
    cache_path: Path,
    user_id: str,
    location: UserLocationRecord,
) -> bool:
    if not connection_type_is_wifi(location.connection_type):
        return False
    users = list_users(cache_path)
    home_bssid = next(
        (row.home_wifi_bssid for row in users if row.user_id == user_id),
        None,
    )
    normalized_home = normalize_wifi_bssid(home_bssid)
    if normalized_home is None:
        return False
    if not wifi_bssids_match(location.wifi_bssid, normalized_home):
        return False
    settings = load_settings_location()
    geofences = list_geofences(cache_path)
    return bool(wifi_home_geofence_ids(settings, geofences))
