"""Hermetic tests for cron schedule helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.cron_schedule import (
    fired_on_same_local_calendar_day,
    local_calendar_date,
    next_scheduled_evaluate_at,
    next_windowed_repeat_evaluate_at,
    validate_schedule_cron_expression,
)


def test_fired_on_same_local_calendar_day_false_after_midnight() -> None:
    tz = ZoneInfo("America/New_York")
    evening = datetime(2023, 11, 4, 23, 0, tzinfo=tz).timestamp()
    next_day = datetime(2023, 11, 5, 0, 15, tzinfo=tz).timestamp()
    assert not fired_on_same_local_calendar_day(evening, next_day, tz)


def test_fired_on_same_local_calendar_day_matches_same_evening() -> None:
    tz = ZoneInfo("America/New_York")
    evening = datetime(2023, 11, 4, 23, 0, tzinfo=tz).timestamp()
    later = datetime(2023, 11, 4, 23, 45, tzinfo=tz).timestamp()
    assert fired_on_same_local_calendar_day(evening, later, tz)


def test_local_calendar_date_uses_home_timezone() -> None:
    tz = ZoneInfo("America/New_York")
    # 2024-01-15 04:30 UTC is still 2024-01-14 evening in New York (EST).
    epoch = datetime(2024, 1, 15, 4, 30, tzinfo=UTC).timestamp()
    assert local_calendar_date(epoch, tz).isoformat() == "2024-01-14"


def test_next_scheduled_evaluate_at_returns_future_epoch() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 6, 15, 12, 7, 0, tzinfo=tz)
    next_at = next_scheduled_evaluate_at("*/15 * * * *", now, tz)
    assert next_at > now.timestamp()


def test_next_scheduled_evaluate_at_returns_now_when_boundary_matches() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=tz)
    next_at = next_scheduled_evaluate_at("0 12 * * *", now, tz, due_if_matching=True)
    assert next_at == now.timestamp()


def test_next_windowed_repeat_evaluate_at_waits_for_anchor() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 7, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    before = anchor - timedelta(minutes=5)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=before,
        timezone=tz,
        window_end=window_end,
    )
    assert next_at == anchor.timestamp()


def test_next_windowed_repeat_evaluate_at_uses_croniter_after_anchor() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 7, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    after_anchor = anchor + timedelta(minutes=2)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=after_anchor,
        timezone=tz,
        window_end=window_end,
        due_if_inside_window=True,
    )
    assert next_at == after_anchor.timestamp()


def test_next_windowed_repeat_evaluate_at_after_anchor_advances_to_first_tick() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 7, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    after_anchor = anchor + timedelta(minutes=2)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=after_anchor,
        timezone=tz,
        window_end=window_end,
    )
    assert next_at == datetime(2024, 6, 15, 20, 10, 0, tzinfo=tz).timestamp()


def test_next_windowed_repeat_evaluate_at_at_anchor_evaluates_immediately() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 7, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=anchor,
        timezone=tz,
        window_end=window_end,
        due_if_inside_window=True,
    )
    assert next_at == anchor.timestamp()


def test_next_windowed_repeat_evaluate_at_schedules_tail_before_window_end() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 0, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    late_evening = datetime(2024, 6, 15, 23, 55, 0, tzinfo=tz)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=late_evening,
        timezone=tz,
        window_end=window_end,
        due_if_inside_window=True,
    )
    assert next_at == late_evening.timestamp()


def test_next_windowed_repeat_evaluate_at_returns_none_after_window() -> None:
    tz = ZoneInfo("America/New_York")
    anchor = datetime(2024, 6, 15, 20, 7, 0, tzinfo=tz)
    window_end = datetime(2024, 6, 16, 0, 0, 0, tzinfo=tz)
    after_window = window_end + timedelta(minutes=1)
    next_at = next_windowed_repeat_evaluate_at(
        "*/10 * * * *",
        anchor=anchor,
        now=after_window,
        timezone=tz,
        window_end=window_end,
    )
    assert next_at is None


def test_validate_schedule_cron_expression_accepts_five_field_cron() -> None:
    validate_schedule_cron_expression("*/15 * * * *")


def test_validate_schedule_cron_expression_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_schedule_cron_expression("")


def test_validate_schedule_cron_expression_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="5-field cron"):
        validate_schedule_cron_expression("not a cron")


def test_validate_schedule_cron_expression_rejects_six_field_cron() -> None:
    with pytest.raises(ValueError, match="5-field cron"):
        validate_schedule_cron_expression("0 0 1 1 1 2020")
