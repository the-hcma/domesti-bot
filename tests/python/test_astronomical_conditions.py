"""Hermetic tests for astronomical rule window boundaries.

Logic mirrors ``web/src/astronomical-conditions.ts`` (minute-of-day model).
"""

from __future__ import annotations

MINUTES_PER_DAY = 24 * 60


def is_in_after_sunset_window_at(
    now_minutes: int,
    sunset_minutes: int,
    offset_minutes: int,
) -> bool:
    start = sunset_minutes + offset_minutes
    if start >= MINUTES_PER_DAY:
        return False
    return now_minutes >= start and now_minutes < MINUTES_PER_DAY


def is_in_before_sunrise_window_at(
    now_minutes: int,
    sunrise_minutes: int,
    offset_minutes: int,
) -> bool:
    end = sunrise_minutes + offset_minutes
    return now_minutes >= 0 and now_minutes < end


def is_in_before_sunset_window_at(
    now_minutes: int,
    sunrise_minutes: int,
    sunset_minutes: int,
    offset_minutes: int,
) -> bool:
    end = sunset_minutes + offset_minutes
    if end >= MINUTES_PER_DAY:
        return False
    return now_minutes >= sunrise_minutes and now_minutes < end


def test_after_sunset_window_is_sunset_through_midnight() -> None:
    sunset = 20 * 60  # 20:00
    assert is_in_after_sunset_window_at(21 * 60, sunset, 0) is True
    assert is_in_after_sunset_window_at(23 * 60 + 59, sunset, 0) is True
    assert is_in_after_sunset_window_at(0, sunset, 0) is False
    assert is_in_after_sunset_window_at(6 * 60, sunset, 0) is False
    assert is_in_after_sunset_window_at(19 * 60 + 30, sunset, 0) is False


def test_before_sunrise_window_is_midnight_through_sunrise() -> None:
    sunrise = 6 * 60 + 15  # 06:15
    assert is_in_before_sunrise_window_at(0, sunrise, 0) is True
    assert is_in_before_sunrise_window_at(3 * 60, sunrise, 0) is True
    assert is_in_before_sunrise_window_at(6 * 60 + 14, sunrise, 0) is True
    assert is_in_before_sunrise_window_at(6 * 60 + 15, sunrise, 0) is False
    assert is_in_before_sunrise_window_at(22 * 60, sunrise, 0) is False


def test_before_sunset_window_is_sunrise_through_sunset() -> None:
    sunrise = 6 * 60  # 06:00
    sunset = 20 * 60  # 20:00
    offset = -25
    end = sunset + offset  # 19:35
    assert is_in_before_sunset_window_at(0, sunrise, sunset, offset) is False
    assert is_in_before_sunset_window_at(sunrise - 1, sunrise, sunset, offset) is False
    assert is_in_before_sunset_window_at(sunrise, sunrise, sunset, offset) is True
    assert is_in_before_sunset_window_at(12 * 60, sunrise, sunset, offset) is True
    assert is_in_before_sunset_window_at(end - 1, sunrise, sunset, offset) is True
    assert is_in_before_sunset_window_at(end, sunrise, sunset, offset) is False
    assert is_in_before_sunset_window_at(22 * 60, sunrise, sunset, offset) is False


def test_before_sunset_window_closed_when_offset_overflows_past_midnight() -> None:
    # A large positive offset can push sunset + offset past 24:00, which this
    # minute-of-day model can't represent as "closes tomorrow" — mirrors
    # is_in_after_sunset_window_at's own MINUTES_PER_DAY guard by reporting
    # the window closed rather than silently "open" for every minute of the
    # day (since now_minutes never reaches an end >= MINUTES_PER_DAY).
    sunrise = 6 * 60  # 06:00
    sunset = 20 * 60  # 20:00
    offset = 300  # nominal close would be 01:00 the next day
    assert is_in_before_sunset_window_at(12 * 60, sunrise, sunset, offset) is False
    assert is_in_before_sunset_window_at(23 * 60, sunrise, sunset, offset) is False
