"""Daily cron materialization for local_time_window eligibility wakes."""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from app.api.schemas import LocalTimeWindowCondition, RuleOut
from app.astronomical_schedule import cron_expression_for_local_datetime
from app.device_enums import RuleTrigger

_HHMM_RE_PARTS = 2


def extract_top_level_local_time_window(rule: RuleOut) -> LocalTimeWindowCondition | None:
    """Return the single top-level ``local_time_window``, if present."""
    windows: list[LocalTimeWindowCondition] = [
        condition for condition in rule.conditions.all if isinstance(condition, LocalTimeWindowCondition)
    ]
    if len(windows) != 1:
        return None
    return windows[0]


def local_time_window_start_datetime(
    window: LocalTimeWindowCondition,
    *,
    local_date: date,
    timezone: ZoneInfo,
) -> datetime | None:
    """Return today's window-open instant from ``start_hhmm`` in ``timezone``."""
    parsed = _parse_hhmm_parts(window.start_hhmm)
    if parsed is None:
        return None
    hour, minute = parsed
    return datetime.combine(local_date, time(hour=hour, minute=minute), tzinfo=timezone)


def materialize_local_time_window_cron(
    rule: RuleOut,
    *,
    timezone: ZoneInfo,
    now: datetime,
) -> str | None:
    """Return today's once-per-day cron for a local_time_window eligibility rule."""
    window = extract_top_level_local_time_window(rule)
    if window is None:
        return None
    local_now = now.astimezone(timezone) if now.tzinfo is not None else now.replace(tzinfo=timezone)
    start_dt = local_time_window_start_datetime(
        window,
        local_date=local_now.date(),
        timezone=timezone,
    )
    if start_dt is None:
        return None
    return cron_expression_for_local_datetime(start_dt)


def uses_local_time_window_eligibility_wake(rule: RuleOut) -> bool:
    """True when dwell/device_state rules need a one-shot eval at window open.

    Implicit eligibility (no ``scheduled`` trigger, no ``schedule_cron``): the
    evaluator materializes today's ``local_time_window`` start and evaluates once
    when that instant is due. Co-equal with ``dwell_satisfied`` and
    ``device_state`` wake-ups — not a hand-rolled clock cron.
    """
    if RuleTrigger.SCHEDULED in rule.triggers:
        return False
    if extract_top_level_local_time_window(rule) is None:
        return False
    if (rule.schedule_cron or "").strip() != "":
        return False
    return RuleTrigger.DEVICE_STATE in rule.triggers or RuleTrigger.DWELL_SATISFIED in rule.triggers


def uses_local_time_window_materialized_schedule(rule: RuleOut) -> bool:
    """True when the evaluator materializes a daily window-open cron for ``rule``."""
    return uses_local_time_window_eligibility_wake(rule)


def _parse_hhmm_parts(hhmm: str) -> tuple[int, int] | None:
    trimmed = hhmm.strip()
    parts = trimmed.split(":")
    if len(parts) != _HHMM_RE_PARTS:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour, minute
