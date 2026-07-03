"""Curated dwell watches derived from enabled ``dwell_satisfied`` rules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from app.api.schemas import (
    RuleOut,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import iter_dwell_for_s_conditions


class DwellDirection(StrEnum):
    """Which geofence streak a dwell watch tracks."""

    INSIDE = "inside"
    OUTSIDE = "outside"


@dataclass(frozen=True, slots=True)
class DwellWatch:
    """One roster user/geofence dwell threshold referenced by rules."""

    direction: DwellDirection
    geofence_id: str
    min_s: int
    rule_ids: frozenset[str]
    rule_user_id: str


@dataclass(frozen=True, slots=True)
class DwellWatchIndex:
    """Sparse index of dwell watches for ``dwell_satisfied`` rules only."""

    watches: tuple[DwellWatch, ...]

    def watches_for_roster_user(
        self,
        roster_user_id: str,
        resolve_user_id: Callable[[str], str | None],
    ) -> tuple[DwellWatch, ...]:
        matched: list[DwellWatch] = []
        for watch in self.watches:
            resolved = resolve_user_id(watch.rule_user_id)
            if resolved == roster_user_id:
                matched.append(watch)
        return tuple(matched)


def build_dwell_watch_index(rules: list[RuleOut]) -> DwellWatchIndex:
    """Build watches only for enabled rules whose triggers include ``dwell_satisfied``."""
    merged: dict[tuple[str, str, DwellDirection, int], set[str]] = {}
    for rule in rules:
        if not rule.enabled or RuleTrigger.DWELL_SATISFIED not in rule.triggers:
            continue
        for condition in iter_dwell_for_s_conditions(rule.conditions.all):
            _merge_dwell_watch_entries(merged, condition, rule.id)
    watches = [
        DwellWatch(
            direction=key[2],
            geofence_id=key[1],
            min_s=key[3],
            rule_ids=frozenset(rule_ids),
            rule_user_id=key[0],
        )
        for key, rule_ids in sorted(merged.items())
    ]
    return DwellWatchIndex(watches=tuple(watches))


def _merge_dwell_watch_entries(
    merged: dict[tuple[str, str, DwellDirection, int], set[str]],
    condition: UsersInsideGeofenceForSCondition | UsersOutsideGeofenceForSCondition,
    rule_id: str,
) -> None:
    if isinstance(condition, UsersInsideGeofenceForSCondition):
        direction = DwellDirection.INSIDE
        min_s = condition.min_inside_s
    else:
        direction = DwellDirection.OUTSIDE
        min_s = condition.min_outside_s
    for rule_user_id in condition.user_ids:
        trimmed = rule_user_id.strip()
        if trimmed == "":
            continue
        key = (trimmed, condition.geofence_id, direction, min_s)
        merged.setdefault(key, set()).add(rule_id)
