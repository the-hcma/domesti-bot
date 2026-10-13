"""Plain-language sunrise / sunset wording shared by email, log and status text."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    AllConditionsCondition,
    BeforeSunriseCondition,
    BeforeSunsetCondition,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
    RulesSunOut,
    SettingsLocationOut,
)
from app.astronomical_phrasing import (
    anchor_clause,
    describe_astronomical_condition,
    describe_rule_sun_timing,
    format_clock_time,
    format_duration_minutes,
    offset_phrase,
    rule_has_astronomical_condition,
    unreachable_window_reason,
)
from app.device_enums import RuleTrigger
from app.rule_evaluator import _sun_timing_notes_for_rule  # pyright: ignore[reportPrivateUsage]

_TZ = ZoneInfo("America/New_York")
_SUN = RulesSunOut(
    is_dark=False,
    sunrise_at="2026-10-13T11:20:00Z",  # 7:20 AM EDT
    sunset_at="2026-10-13T22:32:00Z",  # 6:32 PM EDT
)


def _rule(*conditions: RuleConditionOut) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=list(conditions)),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="r1",
        label="Lamps",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="*/10 * * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )


@pytest.mark.parametrize(
    ("minutes", "expected"),
    [
        (1, "1 minute"),
        (25, "25 minutes"),
        (60, "1 hour"),
        (90, "1 hour 30 minutes"),
        (121, "2 hours 1 minute"),
    ],
)
def test_format_duration_minutes_uses_singular_and_hours(minutes: int, expected: str) -> None:
    assert format_duration_minutes(minutes) == expected


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (-25, "25 minutes before sunset"),
        (30, "30 minutes after sunset"),
        (0, "sunset"),
    ],
)
def test_offset_phrase_reads_direction_from_sign(offset: int, expected: str) -> None:
    assert offset_phrase(event="sunset", offset_minutes=offset) == expected


def test_format_clock_time_uses_twelve_hour_clock() -> None:
    assert format_clock_time(datetime(2026, 10, 13, 0, 5, tzinfo=_TZ)) == "12:05 AM"
    assert format_clock_time(datetime(2026, 10, 13, 18, 32, tzinfo=_TZ)) == "6:32 PM"


def test_anchor_clause_names_event_time_and_resulting_time() -> None:
    sunset = datetime(2026, 10, 13, 18, 32, tzinfo=_TZ)
    assert (
        anchor_clause(event="sunset", event_at=sunset, offset_minutes=-25)
        == "6:07 PM, 25 minutes before sunset at 6:32 PM"
    )
    assert anchor_clause(event="sunset", event_at=sunset, offset_minutes=0) == "6:32 PM, at sunset"


def test_after_sunset_sentence_matches_user_example() -> None:
    note = describe_astronomical_condition(
        AfterSunsetCondition(type="after_sunset", offset_minutes=-25),
        sun=_SUN,
        timezone=_TZ,
    )
    assert note is not None
    assert note.sentence == (
        "Sunset today is at 6:32 PM. The evening window opens at 6:07 PM (25 minutes before sunset)"
        " and runs until midnight."
    )
    assert note.compact == (
        "evening window opens at 6:07 PM (25 minutes before sunset), until midnight; sunset 6:32 PM"
    )


def test_after_sunset_without_offset_says_opens_at_sunset() -> None:
    note = describe_astronomical_condition(
        AfterSunsetCondition(type="after_sunset", offset_minutes=0),
        sun=_SUN,
        timezone=_TZ,
    )
    assert note is not None
    assert "The evening window opens at sunset and runs until midnight." in note.sentence


def test_before_sunrise_sentence_names_closing_time() -> None:
    note = describe_astronomical_condition(
        BeforeSunriseCondition(type="before_sunrise", offset_minutes=10),
        sun=_SUN,
        timezone=_TZ,
    )
    assert note is not None
    assert note.sentence == (
        "Sunrise today is at 7:20 AM. The morning window runs from midnight until 7:30 AM (10 minutes after sunrise)."
    )


def test_before_sunset_sentence_mentions_sunrise_and_closing_time() -> None:
    note = describe_astronomical_condition(
        BeforeSunsetCondition(type="before_sunset", offset_minutes=-25),
        sun=_SUN,
        timezone=_TZ,
    )
    assert note is not None
    assert note.sentence == (
        "Sunrise today is at 7:20 AM and sunset is at 6:32 PM. The daytime window runs from sunrise until"
        " 6:07 PM (25 minutes before sunset)."
    )


def test_describe_astronomical_condition_ignores_other_condition_types() -> None:
    other = AllConditionsCondition(type="all", conditions=[])
    assert describe_astronomical_condition(other, sun=_SUN, timezone=_TZ) is None


def test_rule_sun_timing_finds_nested_astronomical_conditions() -> None:
    nested = AllConditionsCondition(
        type="all",
        conditions=[AfterSunsetCondition(type="after_sunset", offset_minutes=-25)],
    )
    rule = _rule(nested)
    assert rule_has_astronomical_condition(rule) is True
    notes = describe_rule_sun_timing(rule, sun=_SUN, timezone=_TZ)
    assert len(notes) == 1
    assert "6:07 PM" in notes[0].sentence


def test_rule_without_astronomical_conditions_has_no_sun_timing() -> None:
    rule = _rule(AllConditionsCondition(type="all", conditions=[]))
    assert rule_has_astronomical_condition(rule) is False
    assert describe_rule_sun_timing(rule, sun=_SUN, timezone=_TZ) == ()


def test_sun_timing_notes_for_rule_use_home_location_sunset() -> None:
    settings = SettingsLocationOut(
        home_label="Home",
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
    )
    rule = _rule(AfterSunsetCondition(type="after_sunset", offset_minutes=-25))
    fired_at = datetime(2026, 10, 13, 17, 52, tzinfo=_TZ).timestamp()
    with patch("app.rule_evaluator.load_settings_location", return_value=settings):
        notes = _sun_timing_notes_for_rule(rule, at_epoch=fired_at)
    assert len(notes) == 1
    assert "25 minutes before sunset" in notes[0].sentence
    assert "The evening window opens at " in notes[0].sentence


def test_sun_timing_notes_for_rule_skip_settings_when_rule_has_no_sun_condition() -> None:
    rule = _rule(AllConditionsCondition(type="all", conditions=[]))
    with patch("app.rule_evaluator.load_settings_location", side_effect=AssertionError("must not load")):
        assert _sun_timing_notes_for_rule(rule, at_epoch=0.0) == ()


def test_after_sunset_offset_past_midnight_says_window_does_not_open() -> None:
    condition = AfterSunsetCondition(type="after_sunset", offset_minutes=360)
    note = describe_astronomical_condition(condition, sun=_SUN, timezone=_TZ)
    assert note is not None
    assert note.sentence == (
        "Sunset today is at 6:32 PM. The evening window would open at 12:32 AM the next day"
        " (6 hours after sunset), which is at or past midnight, so it does not open today."
    )
    assert note.compact == note.sentence


def test_before_sunset_offset_past_midnight_says_window_does_not_apply() -> None:
    reason = unreachable_window_reason(
        BeforeSunsetCondition(type="before_sunset", offset_minutes=360),
        sun=_SUN,
        timezone=_TZ,
    )
    assert reason is not None
    assert "past midnight" in reason


def test_before_sunrise_offset_before_midnight_says_window_does_not_apply() -> None:
    reason = unreachable_window_reason(
        BeforeSunriseCondition(type="before_sunrise", offset_minutes=-500),
        sun=_SUN,
        timezone=_TZ,
    )
    assert reason is not None
    assert "does not apply today" in reason


def test_reachable_windows_have_no_unreachable_reason() -> None:
    for condition in (
        AfterSunsetCondition(type="after_sunset", offset_minutes=-25),
        BeforeSunriseCondition(type="before_sunrise", offset_minutes=10),
        BeforeSunsetCondition(type="before_sunset", offset_minutes=-25),
    ):
        assert unreachable_window_reason(condition, sun=_SUN, timezone=_TZ) is None


def test_sun_timing_notes_for_rule_return_empty_when_settings_cannot_load() -> None:
    rule = _rule(AfterSunsetCondition(type="after_sunset", offset_minutes=-25))
    with patch("app.rule_evaluator.load_settings_location", side_effect=ValueError("settings unreadable")):
        assert _sun_timing_notes_for_rule(rule, at_epoch=0.0) == ()


def test_before_sunset_closing_before_sunrise_says_window_does_not_apply() -> None:
    reason = unreachable_window_reason(
        BeforeSunsetCondition(type="before_sunset", offset_minutes=-900),
        sun=_SUN,
        timezone=_TZ,
    )
    assert reason == (
        "Sunrise today is at 7:20 AM and sunset is at 6:32 PM. The daytime window would close at"
        " 3:32 AM (15 hours before sunset), which is at or before sunrise, so it does not apply today."
    )


def test_before_sunset_closing_exactly_at_sunrise_is_unreachable() -> None:
    # 6:32 PM minus 11 h 12 min is exactly the 7:20 AM sunrise.
    reason = unreachable_window_reason(
        BeforeSunsetCondition(type="before_sunset", offset_minutes=-672),
        sun=_SUN,
        timezone=_TZ,
    )
    assert reason is not None
    assert "at or before sunrise" in reason
