"""Hermetic tests for astronomical schedule materialization."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    BeforeSunriseCondition,
    RuleConditionsOut,
    RuleOut,
    RulesSunOut,
)
from app.astronomical_schedule import (
    astronomical_anchor_datetime,
    cron_expression_for_local_datetime,
    extract_astronomical_anchor,
    materialize_astronomical_cron,
    uses_astronomical_schedule,
)


def _scheduled_rule(*, schedule_cron: str | None) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-15,
                    window_end="midnight",
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-anchor",
        label="Evening anchor",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron=schedule_cron,
        trigger="scheduled",
    )


def test_extract_astronomical_anchor_returns_single_top_level_anchor() -> None:
    anchor = extract_astronomical_anchor(_scheduled_rule(schedule_cron=None))
    assert anchor is not None
    assert anchor.condition_type == "after_sunset"
    assert anchor.offset_minutes == -15


def test_uses_astronomical_schedule_when_anchor_without_cron() -> None:
    assert uses_astronomical_schedule(_scheduled_rule(schedule_cron=None)) is True


def test_uses_astronomical_schedule_false_when_polling_cron_present() -> None:
    assert uses_astronomical_schedule(_scheduled_rule(schedule_cron="*/10 * * * *")) is False


def test_astronomical_anchor_datetime_applies_offset_before_sunset() -> None:
    tz = ZoneInfo("America/New_York")
    sun = RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )
    anchor = extract_astronomical_anchor(_scheduled_rule(schedule_cron=None))
    assert anchor is not None
    anchor_dt = astronomical_anchor_datetime(anchor, sun, tz)
    expected = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(tz) - timedelta(
        minutes=15,
    )
    assert anchor_dt == expected


def test_materialize_astronomical_cron_builds_daily_cron_expression() -> None:
    tz = ZoneInfo("America/New_York")
    sun = RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )
    cron = materialize_astronomical_cron(
        _scheduled_rule(schedule_cron=None),
        sun=sun,
        timezone=tz,
    )
    anchor_dt = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(tz) - timedelta(
        minutes=15,
    )
    assert cron == cron_expression_for_local_datetime(anchor_dt)


def test_rule_out_allows_astronomical_schedule_without_cron() -> None:
    rule = _scheduled_rule(schedule_cron=None)
    assert rule.schedule_cron is None


def test_rule_out_rejects_multiple_top_level_astronomical_conditions() -> None:
    with pytest.raises(ValueError, match="at most one top-level"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    AfterSunsetCondition(
                        type="after_sunset",
                        offset_minutes=0,
                        window_end="midnight",
                    ),
                    BeforeSunriseCondition(
                        type="before_sunrise",
                        offset_minutes=0,
                        window_start="midnight",
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="too-many-anchors",
            label="Too many anchors",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            trigger="scheduled",
        )
