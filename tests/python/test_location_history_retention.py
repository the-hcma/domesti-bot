"""Tests for location-history retention policy."""

from __future__ import annotations

import time

from app.location_history_retention import (
    DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
    DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
    default_location_history_retention,
    retained_history_row_ids,
)


def test_default_retention_keeps_union_of_age_window_and_recent_count() -> None:
    retention = default_location_history_retention()
    now = 1_700_000_000.0
    rows = [
        (1, now - 100_000.0),
        (2, now - 50_000.0),
        (3, now - 10_000.0),
        (4, now - 3_600.0),
        (5, now - 100.0),
    ]
    keep_ids = retained_history_row_ids(rows, now=now, retention=retention)
    assert keep_ids == {1, 2, 3, 4, 5}


def test_retention_keeps_at_least_min_keep_count_when_older_than_window() -> None:
    retention = default_location_history_retention()
    now = 1_700_000_000.0
    rows = [(index, now - (index * DEFAULT_LOCATION_HISTORY_MAX_AGE_S)) for index in range(1, 26)]
    keep_ids = retained_history_row_ids(rows, now=now, retention=retention)
    assert len(keep_ids) == DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT
    assert keep_ids == set(range(1, DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT + 1))


def test_unlimited_retention_keeps_every_row() -> None:
    retention = default_location_history_retention()
    unlimited = retention.__class__(
        max_age_s=retention.max_age_s,
        min_keep_count=retention.min_keep_count,
        unlimited=True,
    )
    rows = [(1, 0.0), (2, 1.0), (3, 2.0)]
    assert retained_history_row_ids(rows, now=time.time(), retention=unlimited) == {1, 2, 3}
