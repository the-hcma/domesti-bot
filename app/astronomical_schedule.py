"""Daily cron materialization for scheduled rules tied to sunrise/sunset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterSunsetCondition,
    BeforeSunriseCondition,
    RuleOut,
    RulesSunOut,
    SettingsLocationOut,
)
from app.cron_schedule import (
    next_windowed_repeat_evaluate_at,
    validate_schedule_cron_expression,
)


@dataclass(frozen=True)
class AstronomicalAnchor:
    """One astronomical evaluation anchor extracted from rule conditions."""

    condition_type: Literal["after_sunset", "before_sunrise"]
    offset_minutes: int


def astronomical_repeat_cron(rule: RuleOut) -> str | None:
    """Return the repeat cron when an astronomical rule also polls on a cadence."""
    cron = (rule.schedule_cron or "").strip()
    return cron if cron != "" else None


def astronomical_evaluation_window(
    anchor: AstronomicalAnchor,
    *,
    anchor_dt: datetime,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return the local ``[start, end)`` evaluation window for ``anchor_dt``."""
    local_anchor = anchor_dt.astimezone(timezone)
    if anchor.condition_type == "after_sunset":
        window_end = local_midnight_after(local_anchor.date(), timezone)
        return local_anchor, window_end
    window_start = datetime.combine(local_anchor.date(), time.min, tzinfo=timezone)
    return window_start, local_anchor


def extract_astronomical_anchor(rule: RuleOut) -> AstronomicalAnchor | None:
    """Return the single top-level sunrise/sunset anchor, if present."""
    anchors: list[AstronomicalAnchor] = []
    for condition in rule.conditions.all:
        if isinstance(condition, AfterSunsetCondition):
            anchors.append(
                AstronomicalAnchor("after_sunset", condition.offset_minutes),
            )
        elif isinstance(condition, BeforeSunriseCondition):
            anchors.append(
                AstronomicalAnchor("before_sunrise", condition.offset_minutes),
            )
    if len(anchors) != 1:
        return None
    return anchors[0]


def uses_astronomical_repeat_schedule(rule: RuleOut) -> bool:
    """True when a scheduled rule anchors on sun events and repeats on ``schedule_cron``."""
    return (
        uses_astronomical_schedule(rule)
        and astronomical_repeat_cron(rule) is not None
    )


def uses_astronomical_schedule(rule: RuleOut) -> bool:
    """True when a scheduled rule includes a top-level sunrise/sunset anchor."""
    if rule.trigger != "scheduled":
        return False
    return extract_astronomical_anchor(rule) is not None


def astronomical_anchor_datetime(
    anchor: AstronomicalAnchor,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> datetime:
    """Return the local evaluation instant for ``anchor`` on ``sun``'s calendar day."""
    iso = sun.sunset_at if anchor.condition_type == "after_sunset" else sun.sunrise_at
    base = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone)
    return base + timedelta(minutes=anchor.offset_minutes)


def cron_expression_for_local_datetime(dt: datetime) -> str:
    """Build a once-per-day 5-field cron for a fixed local clock time."""
    cron = f"{dt.minute} {dt.hour} * * *"
    validate_schedule_cron_expression(cron)
    return cron


def local_midnight_after(local_date: date, timezone: ZoneInfo) -> datetime:
    """Return local midnight at the start of the day after ``local_date``."""
    return datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=timezone)


def materialize_astronomical_cron(
    rule: RuleOut,
    *,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> str | None:
    """Return today's materialized cron for an astronomical scheduled rule."""
    anchor = extract_astronomical_anchor(rule)
    if anchor is None:
        return None
    repeat_cron = astronomical_repeat_cron(rule)
    if repeat_cron is not None:
        return repeat_cron
    anchor_dt = astronomical_anchor_datetime(anchor, sun, timezone)
    return cron_expression_for_local_datetime(anchor_dt)


def next_astronomical_repeat_evaluate_at(
    rule: RuleOut,
    *,
    settings: SettingsLocationOut,
    timezone: ZoneInfo,
    now: datetime,
    due_if_inside_window: bool = False,
) -> float:
    """Return the next evaluation epoch for an astronomical rule with repeat cron."""
    from app.rule_conditions import compute_rules_sun_out

    anchor = extract_astronomical_anchor(rule)
    repeat_cron = astronomical_repeat_cron(rule)
    if anchor is None or repeat_cron is None:
        msg = (
            "Expected astronomical scheduled rule with schedule_cron, "
            f"got rule_id={rule.id!r}"
        )
        raise ValueError(msg)

    local_now = now.astimezone(timezone) if now.tzinfo is not None else now.replace(
        tzinfo=timezone,
    )
    sun = compute_rules_sun_out(settings, now=local_now)
    anchor_dt = astronomical_anchor_datetime(anchor, sun, timezone)
    window_start, window_end = astronomical_evaluation_window(
        anchor,
        anchor_dt=anchor_dt,
        timezone=timezone,
    )
    next_at = next_windowed_repeat_evaluate_at(
        repeat_cron,
        anchor=window_start,
        now=local_now,
        timezone=timezone,
        window_end=window_end,
        due_if_inside_window=due_if_inside_window,
    )
    if next_at is not None:
        return next_at
    next_anchor_dt = _anchor_for_local_date(
        anchor,
        settings=settings,
        local_date=local_now.date() + timedelta(days=1),
        timezone=timezone,
    )
    next_window_start, _ = astronomical_evaluation_window(
        anchor,
        anchor_dt=next_anchor_dt,
        timezone=timezone,
    )
    return next_window_start.timestamp()


def schedule_materialized_for_date(local_date: date) -> str:
    """Persisted local calendar stamp for a materialized astronomical schedule."""
    return local_date.isoformat()


def parse_schedule_materialized_for(value: str | None) -> date | None:
    """Parse ``schedule_materialized_for`` from SQLite."""
    if value is None or value.strip() == "":
        return None
    return date.fromisoformat(value.strip())


def _anchor_for_local_date(
    anchor: AstronomicalAnchor,
    *,
    settings: SettingsLocationOut,
    local_date: date,
    timezone: ZoneInfo,
) -> datetime:
    from app.rule_conditions import compute_rules_sun_out

    noon = datetime.combine(local_date, time(hour=12), tzinfo=timezone)
    sun = compute_rules_sun_out(settings, now=noon)
    return astronomical_anchor_datetime(anchor, sun, timezone)
