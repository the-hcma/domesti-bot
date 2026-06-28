"""Daily cron materialization for scheduled rules tied to sunrise/sunset."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterSunsetCondition,
    BeforeSunriseCondition,
    RuleOut,
    RulesSunOut,
)
from app.cron_schedule import validate_schedule_cron_expression


@dataclass(frozen=True)
class AstronomicalAnchor:
    """One astronomical evaluation anchor extracted from rule conditions."""

    condition_type: Literal["after_sunset", "before_sunrise"]
    offset_minutes: int


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


def uses_astronomical_schedule(rule: RuleOut) -> bool:
    """True when a scheduled rule derives its cron from sunrise/sunset daily."""
    if rule.trigger != "scheduled":
        return False
    if (rule.schedule_cron or "").strip() != "":
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
    anchor_dt = astronomical_anchor_datetime(anchor, sun, timezone)
    return cron_expression_for_local_datetime(anchor_dt)


def schedule_materialized_for_date(local_date: date) -> str:
    """Persisted local calendar stamp for a materialized astronomical schedule."""
    return local_date.isoformat()


def parse_schedule_materialized_for(value: str | None) -> date | None:
    """Parse ``schedule_materialized_for`` from SQLite."""
    if value is None or value.strip() == "":
        return None
    return date.fromisoformat(value.strip())
