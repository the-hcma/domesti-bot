"""Daily cron materialization for scheduled rules tied to sunrise/sunset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterSunsetCondition,
    BeforeSunriseCondition,
    BeforeSunsetCondition,
    RuleOut,
    RulesSunOut,
    SettingsLocationOut,
)
from app.cron_schedule import (
    next_windowed_repeat_evaluate_at,
    validate_schedule_cron_expression,
)
from app.device_enums import RuleTrigger


@dataclass(frozen=True)
class AstronomicalAnchor:
    """One astronomical evaluation anchor extracted from rule conditions."""

    condition_type: Literal["after_sunset", "before_sunrise", "before_sunset"]
    offset_minutes: int


def astronomical_repeat_cron(rule: RuleOut) -> str | None:
    """Return the repeat cron when an astronomical rule also polls on a cadence."""
    cron = (rule.schedule_cron or "").strip()
    return cron if cron != "" else None


def astronomical_evaluation_window(
    anchor: AstronomicalAnchor,
    *,
    anchor_dt: datetime,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> tuple[datetime, datetime]:
    """Return the local ``[start, end)`` evaluation window for ``anchor_dt``.

    ``after_sunset`` opens at the anchor and runs to the following midnight.
    ``before_sunrise`` opens at local midnight and runs up to the anchor.
    ``before_sunset`` opens at sunrise (not midnight — it excludes the
    pre-dawn hours) and runs up to the anchor (``sunset + offset_minutes``).
    """
    local_anchor = anchor_dt.astimezone(timezone)
    if anchor.condition_type == "after_sunset":
        window_end = local_midnight_after(local_anchor.date(), timezone)
        return local_anchor, window_end
    if anchor.condition_type == "before_sunset":
        return _parse_iso_local(sun.sunrise_at, timezone), local_anchor
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
        elif isinstance(condition, BeforeSunsetCondition):
            anchors.append(
                AstronomicalAnchor("before_sunset", condition.offset_minutes),
            )
    if len(anchors) != 1:
        return None
    return anchors[0]


def uses_astronomical_edge_window_open_schedule(rule: RuleOut) -> bool:
    """True when an edge rule also arms with a one-shot presence eval at window start.

    Opt-in via ``triggers: [edge_true, scheduled]`` with a top-level astronomical
    anchor, ``fire_once_per_local_day``, and no repeat ``schedule_cron``.
    """
    return (
        RuleTrigger.EDGE_TRUE in rule.triggers
        and RuleTrigger.SCHEDULED in rule.triggers
        and rule.fire_once_per_local_day
        and extract_astronomical_anchor(rule) is not None
        and astronomical_repeat_cron(rule) is None
    )


def uses_astronomical_eligibility_wake(rule: RuleOut) -> bool:
    """True when dwell/device_state rules need a one-shot eval at sun-window open.

    Implicit eligibility (no ``scheduled`` trigger, no repeat ``schedule_cron``): the
    evaluator materializes today's astronomical anchor (e.g. sunset) and evaluates
    once when that instant is due. Co-equal with ``dwell_satisfied`` and
    ``device_state`` wake-ups — not a cron poll.
    """
    if RuleTrigger.SCHEDULED in rule.triggers:
        return False
    if extract_astronomical_anchor(rule) is None:
        return False
    if astronomical_repeat_cron(rule) is not None:
        return False
    return RuleTrigger.DEVICE_STATE in rule.triggers or RuleTrigger.DWELL_SATISFIED in rule.triggers


def uses_astronomical_materialized_schedule(rule: RuleOut) -> bool:
    """True when the evaluator materializes a daily sun-anchor cron for ``rule``."""
    return uses_astronomical_eligibility_wake(rule) or uses_astronomical_schedule(rule)


def uses_astronomical_repeat_schedule(rule: RuleOut) -> bool:
    """True when a scheduled rule anchors on sun events and repeats on ``schedule_cron``."""
    return (
        RuleTrigger.SCHEDULED in rule.triggers
        and extract_astronomical_anchor(rule) is not None
        and astronomical_repeat_cron(rule) is not None
    )


def uses_astronomical_schedule(rule: RuleOut) -> bool:
    """True when a rule includes a top-level sunrise/sunset evaluation schedule."""
    if extract_astronomical_anchor(rule) is None:
        return False
    return RuleTrigger.SCHEDULED in rule.triggers


def astronomical_anchor_datetime(
    anchor: AstronomicalAnchor,
    sun: RulesSunOut,
    timezone: ZoneInfo,
) -> datetime:
    """Return the local evaluation instant for ``anchor`` on ``sun``'s calendar day."""
    iso = sun.sunset_at if anchor.condition_type in ("after_sunset", "before_sunset") else sun.sunrise_at
    return _parse_iso_local(iso, timezone) + timedelta(minutes=anchor.offset_minutes)


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
        msg = f"Expected astronomical scheduled rule with schedule_cron, got rule_id={rule.id!r}"
        raise ValueError(msg)

    local_now = (
        now.astimezone(timezone)
        if now.tzinfo is not None
        else now.replace(
            tzinfo=timezone,
        )
    )
    sun = compute_rules_sun_out(settings, now=local_now)
    anchor_dt = astronomical_anchor_datetime(anchor, sun, timezone)
    window_start, window_end = astronomical_evaluation_window(
        anchor,
        anchor_dt=anchor_dt,
        sun=sun,
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
    next_sun = _sun_for_local_date(
        settings=settings,
        local_date=local_now.date() + timedelta(days=1),
        timezone=timezone,
    )
    next_anchor_dt = astronomical_anchor_datetime(anchor, next_sun, timezone)
    next_window_start, _ = astronomical_evaluation_window(
        anchor,
        anchor_dt=next_anchor_dt,
        sun=next_sun,
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


def _parse_iso_local(iso: str, timezone: ZoneInfo) -> datetime:
    """Parse a UTC ``Z``-suffixed ISO timestamp into ``timezone``'s local wall clock."""
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone)


def _sun_for_local_date(
    *,
    settings: SettingsLocationOut,
    local_date: date,
    timezone: ZoneInfo,
) -> RulesSunOut:
    from app.rule_conditions import compute_rules_sun_out

    noon = datetime.combine(local_date, time(hour=12), tzinfo=timezone)
    return compute_rules_sun_out(settings, now=noon)
