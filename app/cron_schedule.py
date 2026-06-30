"""Cron schedule helpers for scheduled automation rules."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from croniter import croniter


def _local_datetime(dt: datetime, timezone: ZoneInfo) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone)
    return dt.astimezone(timezone)


def fired_on_same_local_calendar_day(
    last_fired_at: float | None,
    now_epoch: float,
    timezone: ZoneInfo,
) -> bool:
    """True when ``last_fired_at`` and ``now_epoch`` fall on the same local date."""
    if last_fired_at is None:
        return False
    return local_calendar_date(last_fired_at, timezone) == local_calendar_date(
        now_epoch,
        timezone,
    )


def local_calendar_date(epoch_seconds: float, timezone: ZoneInfo) -> date:
    """Return the local calendar date for ``epoch_seconds`` in ``timezone``."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone).date()


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
    local_now = _local_datetime(now, timezone)
    iterator = croniter(cron_expr, local_now)
    if due_if_matching and croniter.match(cron_expr, local_now):
        return local_now.timestamp()
    next_local = iterator.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=timezone)
    return next_local.timestamp()


def next_windowed_repeat_evaluate_at(
    repeat_cron: str,
    *,
    anchor: datetime,
    now: datetime,
    timezone: ZoneInfo,
    window_end: datetime,
    due_if_inside_window: bool = False,
) -> float | None:
    """Return the next evaluation instant inside ``[anchor, window_end)``.

    ``anchor`` is always the first evaluation of the window. Later evaluations
    follow ``repeat_cron`` via croniter seeded from ``anchor``. Returns ``None``
    when ``now`` is at or past ``window_end`` so the caller can schedule the
    next day's anchor.

    When ``due_if_inside_window`` is true and ``now`` is inside the window but
    not yet on a cron tick, return ``now`` so a cold start still evaluates
    promptly. After a due evaluation, pass ``due_if_inside_window=False`` so the
    next scheduled instant is strictly in the future.
    """
    local_now = _local_datetime(now, timezone)
    local_anchor = _local_datetime(anchor, timezone)
    local_end = _local_datetime(window_end, timezone)

    if local_now < local_anchor:
        return local_anchor.timestamp()

    if local_now == local_anchor and due_if_inside_window:
        return local_anchor.timestamp()

    if local_now >= local_end:
        return None

    iterator = croniter(repeat_cron, local_anchor)
    first_tick = iterator.get_next(datetime)
    if first_tick.tzinfo is None:
        first_tick = first_tick.replace(tzinfo=timezone)
    else:
        first_tick = first_tick.astimezone(timezone)
    if first_tick >= local_end:
        if due_if_inside_window and local_now < local_end:
            return local_now.timestamp()
        return None
    if local_now < first_tick:
        if due_if_inside_window:
            return local_now.timestamp()
        return first_tick.timestamp()

    while True:
        candidate = iterator.get_next(datetime)
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=timezone)
        else:
            candidate = candidate.astimezone(timezone)
        if candidate >= local_end:
            if due_if_inside_window and local_now < local_end:
                return local_now.timestamp()
            return None
        if candidate > local_now:
            return candidate.timestamp()


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
