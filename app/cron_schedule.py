"""Cron schedule helpers for scheduled automation rules."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def next_scheduled_evaluate_at(
    cron_expr: str,
    now: datetime,
    timezone: ZoneInfo,
    *,
    due_if_matching: bool = False,
) -> float:
    """Return the next cron match after ``now`` as a UTC epoch timestamp.

    When ``due_if_matching`` is true and ``now`` satisfies the cron expression,
    return ``now`` so a restart on a schedule boundary still evaluates promptly.
    """
    local_now = now.astimezone(timezone) if now.tzinfo is not None else now.replace(
        tzinfo=timezone,
    )
    iterator = croniter(cron_expr, local_now)
    if due_if_matching and croniter.match(cron_expr, local_now):
        return local_now.timestamp()
    next_local = iterator.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=timezone)
    return next_local.timestamp()


def validate_schedule_cron_expression(cron_expr: str) -> None:
    """Raise ``ValueError`` when ``cron_expr`` is not a valid 5-field cron string."""
    trimmed = cron_expr.strip()
    if trimmed == "":
        raise ValueError("Expected non-empty schedule_cron, got empty string")
    fields = trimmed.split()
    if len(fields) != 5:
        raise ValueError(
            "Expected 5-field cron expression (minute hour day month weekday), "
            f"got {len(fields)} fields in {trimmed!r}",
        )
    try:
        croniter(trimmed)
    except (KeyError, ValueError) as exc:
        raise ValueError(
            f"Expected valid 5-field cron expression, got {trimmed!r}: {exc}",
        ) from exc
