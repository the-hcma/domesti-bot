"""Plain-language wording for sunrise / sunset conditions and their offsets.

Rule-fire emails, ``[rules] fired`` log lines and condition status text all
describe astronomical windows. They share these helpers so a reader sees the
event time, the offset in words and the resulting clock time the same way
everywhere, e.g. ``6:07 PM, 25 minutes before sunset at 6:32 PM``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterSunsetCondition,
    AllConditionsCondition,
    AnyConditionsCondition,
    BeforeSunriseCondition,
    BeforeSunsetCondition,
    RuleConditionOut,
    RuleOut,
    RulesSunOut,
)

MINUTES_PER_DAY = 24 * 60
MINUTES_PER_HOUR = 60


@dataclass(frozen=True)
class SunTimingNote:
    """One astronomical condition described for humans."""

    compact: str
    sentence: str


def anchor_clause(
    *,
    event: str,
    event_at: datetime,
    offset_minutes: int,
) -> str:
    """Return ``"6:07 PM, 25 minutes before sunset at 6:32 PM"`` (or ``"sunset at 6:32 PM"``)."""
    event_clock = format_clock_time(event_at)
    if offset_minutes == 0:
        return f"{event} at {event_clock}"
    anchor_clock = format_clock_time(event_at + timedelta(minutes=offset_minutes))
    return f"{anchor_clock}, {offset_phrase(event=event, offset_minutes=offset_minutes)} at {event_clock}"


def describe_astronomical_condition(
    condition: RuleConditionOut,
    *,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> SunTimingNote | None:
    """Describe one sunrise/sunset condition, or ``None`` for any other condition type."""
    unreachable = unreachable_window_reason(condition, sun=sun, timezone=timezone)
    if unreachable is not None:
        return SunTimingNote(compact=unreachable, sentence=unreachable)
    if isinstance(condition, AfterSunsetCondition):
        offset = condition.offset_minutes
        sunset_at = parse_local_iso(sun.sunset_at, timezone)
        opens = _opens_phrase(event="sunset", event_at=sunset_at, offset_minutes=offset)
        return SunTimingNote(
            compact=f"evening window opens {opens}, until midnight; sunset {format_clock_time(sunset_at)}",
            sentence=(
                f"Sunset today is at {format_clock_time(sunset_at)}. "
                f"The evening window opens {opens} and runs until midnight."
            ),
        )
    if isinstance(condition, BeforeSunriseCondition):
        offset = condition.offset_minutes
        sunrise_at = parse_local_iso(sun.sunrise_at, timezone)
        closes = _until_phrase(event="sunrise", event_at=sunrise_at, offset_minutes=offset)
        return SunTimingNote(
            compact=f"morning window midnight until {closes}; sunrise {format_clock_time(sunrise_at)}",
            sentence=(
                f"Sunrise today is at {format_clock_time(sunrise_at)}. "
                f"The morning window runs from midnight until {closes}."
            ),
        )
    if isinstance(condition, BeforeSunsetCondition):
        offset = condition.offset_minutes
        sunrise_at = parse_local_iso(sun.sunrise_at, timezone)
        sunset_at = parse_local_iso(sun.sunset_at, timezone)
        closes = _until_phrase(event="sunset", event_at=sunset_at, offset_minutes=offset)
        sunrise_clock = format_clock_time(sunrise_at)
        sunset_clock = format_clock_time(sunset_at)
        return SunTimingNote(
            compact=f"daytime window sunrise {sunrise_clock} until {closes}; sunset {sunset_clock}",
            sentence=(
                f"Sunrise today is at {sunrise_clock} and sunset is at {sunset_clock}. "
                f"The daytime window runs from sunrise until {closes}."
            ),
        )
    return None


def describe_rule_sun_timing(
    rule: RuleOut,
    *,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> tuple[SunTimingNote, ...]:
    """Describe every sunrise/sunset condition on ``rule``, nested ``all`` / ``any`` included."""
    notes: list[SunTimingNote] = []
    for condition in _walk_conditions(rule.conditions.all):
        note = describe_astronomical_condition(condition, sun=sun, timezone=timezone)
        if note is not None:
            notes.append(note)
    return tuple(notes)


def format_clock_time(moment: datetime) -> str:
    """Return a 12-hour clock label such as ``6:32 PM``."""
    display_hour = moment.hour % 12 or 12
    suffix = "AM" if moment.hour < 12 else "PM"
    return f"{display_hour}:{moment.minute:02d} {suffix}"


def format_duration_minutes(minutes: int) -> str:
    """Return ``1 minute``, ``25 minutes``, ``1 hour`` or ``1 hour 30 minutes`` for ``minutes`` > 0."""
    if minutes < MINUTES_PER_HOUR:
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    hours, remainder = divmod(minutes, MINUTES_PER_HOUR)
    hours_text = f"{hours} hour" if hours == 1 else f"{hours} hours"
    if remainder == 0:
        return hours_text
    return f"{hours_text} {format_duration_minutes(remainder)}"


def offset_phrase(*, event: str, offset_minutes: int) -> str:
    """Return ``25 minutes before sunset``, ``30 minutes after sunset`` or ``sunset`` for zero."""
    if offset_minutes == 0:
        return event
    direction = "before" if offset_minutes < 0 else "after"
    return f"{format_duration_minutes(abs(offset_minutes))} {direction} {event}"


def parse_local_iso(iso: str, timezone: ZoneInfo) -> datetime:
    """Parse a UTC ``Z``-suffixed ISO timestamp into ``timezone``'s wall clock."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone)


def rule_has_astronomical_condition(rule: RuleOut) -> bool:
    """True when ``rule`` has a sunrise/sunset condition at any nesting depth."""
    return any(
        isinstance(condition, (AfterSunsetCondition, BeforeSunriseCondition, BeforeSunsetCondition))
        for condition in _walk_conditions(rule.conditions.all)
    )


def unreachable_window_reason(
    condition: RuleConditionOut,
    *,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> str | None:
    """Explain why an offset window never applies today, mirroring the evaluator's boundaries.

    ``after_sunset`` never opens when ``sunset + offset`` reaches midnight, ``before_sunset``
    never applies when ``sunset + offset`` passes midnight and ``before_sunrise`` never applies
    when ``sunrise + offset`` is at or before midnight. ``None`` means the window can be active.
    """
    if isinstance(condition, AfterSunsetCondition):
        sunset_at = parse_local_iso(sun.sunset_at, timezone)
        if _minutes_of_day(sunset_at) + condition.offset_minutes >= MINUTES_PER_DAY:
            phrase = offset_phrase(event="sunset", offset_minutes=condition.offset_minutes)
            return (
                f"Sunset today is at {format_clock_time(sunset_at)}. "
                f"The evening window would open {phrase},"
                " which is at or past midnight, so it does not open today."
            )
    if isinstance(condition, BeforeSunriseCondition):
        sunrise_at = parse_local_iso(sun.sunrise_at, timezone)
        if _minutes_of_day(sunrise_at) + condition.offset_minutes <= 0:
            phrase = offset_phrase(event="sunrise", offset_minutes=condition.offset_minutes)
            return (
                f"Sunrise today is at {format_clock_time(sunrise_at)}. "
                f"The morning window would close {phrase},"
                " which is at or before midnight, so it does not apply today."
            )
    if isinstance(condition, BeforeSunsetCondition):
        sunset_at = parse_local_iso(sun.sunset_at, timezone)
        if _minutes_of_day(sunset_at) + condition.offset_minutes > MINUTES_PER_DAY:
            phrase = offset_phrase(event="sunset", offset_minutes=condition.offset_minutes)
            return (
                f"Sunset today is at {format_clock_time(sunset_at)}. "
                f"The daytime window would close {phrase},"
                " which is past midnight, so it does not apply today."
            )
    return None


def _minutes_of_day(moment: datetime) -> int:
    """Return minutes since local midnight."""
    return moment.hour * MINUTES_PER_HOUR + moment.minute


def _opens_phrase(*, event: str, event_at: datetime, offset_minutes: int) -> str:
    """Return ``at sunset`` or ``at 6:07 PM (25 minutes before sunset)``."""
    if offset_minutes == 0:
        return f"at {event}"
    anchor_clock = format_clock_time(event_at + timedelta(minutes=offset_minutes))
    return f"at {anchor_clock} ({offset_phrase(event=event, offset_minutes=offset_minutes)})"


def _until_phrase(*, event: str, event_at: datetime, offset_minutes: int) -> str:
    """Return ``sunrise`` or ``7:30 AM (10 minutes after sunrise)``."""
    if offset_minutes == 0:
        return event
    anchor_clock = format_clock_time(event_at + timedelta(minutes=offset_minutes))
    return f"{anchor_clock} ({offset_phrase(event=event, offset_minutes=offset_minutes)})"


def _walk_conditions(conditions: list[RuleConditionOut]) -> list[RuleConditionOut]:
    """Flatten nested ``all`` / ``any`` conditions depth-first, keeping declaration order."""
    flat: list[RuleConditionOut] = []
    for condition in conditions:
        flat.append(condition)
        if isinstance(condition, (AllConditionsCondition, AnyConditionsCondition)):
            flat.extend(_walk_conditions(condition.conditions))
    return flat
