"""Retention policy for per-user location history."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_LOCATION_HISTORY_MAX_AGE_S = 86_400.0
DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT = 20


@dataclass(frozen=True)
class LocationHistoryRetention:
    max_age_s: float
    min_keep_count: int
    unlimited: bool


def default_location_history_retention() -> LocationHistoryRetention:
    """Return the default keep-more policy (24h window ∪ 20 most recent fixes)."""
    return LocationHistoryRetention(
        max_age_s=DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
        min_keep_count=DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
        unlimited=False,
    )


def location_history_retention_from_settings(
    *,
    max_age_s: float | None,
    min_keep_count: int | None,
    unlimited: bool | int,
) -> LocationHistoryRetention:
    """Build a retention policy from persisted my-tracks settings columns."""
    if bool(unlimited):
        return LocationHistoryRetention(
            max_age_s=DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
            min_keep_count=DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
            unlimited=True,
        )
    return LocationHistoryRetention(
        max_age_s=(
            max_age_s
            if max_age_s is not None
            else DEFAULT_LOCATION_HISTORY_MAX_AGE_S
        ),
        min_keep_count=(
            min_keep_count
            if min_keep_count is not None
            else DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT
        ),
        unlimited=False,
    )


def retained_history_row_ids(
    rows: list[tuple[int, float]],
    *,
    now: float,
    retention: LocationHistoryRetention,
) -> set[int]:
    """Return history row ids to keep for one user.

    ``rows`` must be ``(id, received_at)`` tuples sorted by ``received_at`` descending.
    A location reading is kept when it falls inside the age window **or** ranks
    among the ``min_keep_count`` most recent readings — whichever policy retains
    more rows.
    """
    if retention.unlimited:
        return {row_id for row_id, _received_at in rows}
    keep_ids: set[int] = set()
    cutoff = now - retention.max_age_s
    for row_id, received_at in rows:
        if received_at >= cutoff:
            keep_ids.add(row_id)
    for row_id, _received_at in rows[: retention.min_keep_count]:
        keep_ids.add(row_id)
    return keep_ids
