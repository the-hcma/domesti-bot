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
