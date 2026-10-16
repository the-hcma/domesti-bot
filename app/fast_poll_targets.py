"""Which devices to poll faster: those a turn-on rule watches while its time window is open.

A ``devices_any_transitioned_to_state`` rule can only see a change when the
watcher polls the device, so its reaction lags by up to one poll interval.
This module names the few devices worth polling at :data:`FAST_POLL_INTERVAL_S`
instead of the global interval, and only while the rule's own
``local_time_window`` condition is open. Every other device keeps the global
cadence.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Final
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AllConditionsCondition,
    AnyConditionsCondition,
    DevicesAnyTransitionedToStateCondition,
    LocalTimeWindowCondition,
    RuleConditionOut,
    RuleOut,
    rule_has_device_transition_condition,
)
from app.automation_rules_loader import automation_rules_json_path, load_automation_rules_bundle
from app.device_enums import DeviceFamilyId
from app.device_mac import try_normalize_mac
from app.local_time_schedule import is_local_time_window_open

_LOGGER = logging.getLogger(__name__)

FAST_POLL_INTERVAL_S: Final[float] = 5.0

FastPollTargets = frozenset[tuple[DeviceFamilyId, str]]
FastPollTargetsProvider = Callable[[], FastPollTargets]


class FastPollTargetsReader:
    """Return the devices to poll fast right now, from the rules file on disk.

    The rules file is parsed once and again only when its modification time or
    size changes, so a call is a ``stat`` plus the time-window check, not a full
    parse of every rule. A rules file that fails to load keeps the last good
    answer and is logged once until it loads again, so a typo never switches the
    fast polling off (or on) by accident. Meant to be called from a worker
    thread (the first parse after an edit takes tens of milliseconds).
    """

    def __init__(self, *, now_fn: Callable[[], datetime] | None = None) -> None:
        self._cache_key: tuple[str, int, int] | None = None
        self._last_good: FastPollTargets = frozenset()
        self._load_failed = False
        self._now_fn = now_fn
        self._rules: list[RuleOut] = []
        self._timezone: ZoneInfo | None = None

    def __call__(self) -> FastPollTargets:
        try:
            rules, timezone = self._rules_and_timezone()
            now = self._now_fn() if self._now_fn is not None else datetime.now(timezone)
            targets = fast_poll_targets_at(rules, now=now.astimezone(timezone))
        except Exception:
            if not self._load_failed:
                _LOGGER.exception("[state-watcher] fast-poll targets unchanged: rules failed to load")
            self._load_failed = True
            return self._last_good
        if self._load_failed:
            _LOGGER.info("[state-watcher] fast-poll targets resumed: rules loaded again")
        self._load_failed = False
        self._last_good = targets
        return targets

    def _rules_and_timezone(self) -> tuple[list[RuleOut], ZoneInfo]:
        path = automation_rules_json_path()
        stat = path.stat()
        cache_key = (str(path), stat.st_mtime_ns, stat.st_size)
        if cache_key != self._cache_key or self._timezone is None:
            bundle = load_automation_rules_bundle(path=path)
            self._rules = bundle.rules
            self._timezone = ZoneInfo(bundle.settings_location.timezone)
            self._cache_key = cache_key
        return self._rules, self._timezone


def fast_poll_targets_at(rules: Iterable[RuleOut], *, now: datetime) -> FastPollTargets:
    """Return ``(family, device)`` pairs watched by an enabled transition rule whose window is open.

    ``now`` must be in the rules' local timezone. Device ids are normalized
    MACs when they parse as one, otherwise the stripped reference.
    """
    targets: set[tuple[DeviceFamilyId, str]] = set()
    for rule in rules:
        if not rule.enabled or not rule_has_device_transition_condition(rule):
            continue
        if not _time_gate_open(rule.conditions.all, now):
            continue
        for condition in rule.conditions.all:
            if not isinstance(condition, DevicesAnyTransitionedToStateCondition):
                continue
            for ref in condition.devices:
                targets.add((ref.family_id, fast_poll_device_key(ref.device_id)))
    return frozenset(targets)


def fast_poll_device_key(device_ref: str) -> str:
    """Return the form a rule's device reference takes in a target set (normalized MAC, else stripped)."""
    return try_normalize_mac(device_ref) or device_ref.strip()


def _time_gate_open(conditions: Iterable[RuleConditionOut], now: datetime) -> bool:
    """Whether every ``local_time_window`` gate in ``conditions`` is open at ``now``.

    Windows under ``any`` need only one open child. Any other condition is
    treated as open, so a time gate this module does not understand never
    stops the faster polling; it can only make it run a little longer.
    """
    for condition in conditions:
        if isinstance(condition, LocalTimeWindowCondition):
            if not is_local_time_window_open(condition, now=now):
                return False
        elif isinstance(condition, AllConditionsCondition):
            if not _time_gate_open(condition.conditions, now):
                return False
        elif isinstance(condition, AnyConditionsCondition):
            if not any(_time_gate_open([child], now) for child in condition.conditions):
                return False
    return True
