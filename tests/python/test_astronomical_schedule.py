"""Hermetic tests for astronomical schedule materialization."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    BeforeSunriseCondition,
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    RulesSunOut,
    SettingsLocationOut,
    UsersInsideGeofenceForSCondition,
)
from app.astronomical_schedule import (
    astronomical_anchor_datetime,
    cron_expression_for_local_datetime,
    extract_astronomical_anchor,
    materialize_astronomical_cron,
    next_astronomical_repeat_evaluate_at,
    uses_astronomical_edge_window_open_schedule,
    uses_astronomical_eligibility_wake,
    uses_astronomical_materialized_schedule,
    uses_astronomical_repeat_schedule,
    uses_astronomical_schedule,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.rule_conditions import compute_rules_sun_out


def _before_sunrise_rule(*, schedule_cron: str | None) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
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
        fire_once_per_local_day=True,
        id="morning-anchor",
        label="Morning anchor",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron=schedule_cron,
        triggers=[RuleTrigger.SCHEDULED],
    )


def _edge_window_open_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-25,
                    window_end="midnight",
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-interior",
        label="Evening interior",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE, RuleTrigger.SCHEDULED],
    )


def _eligibility_wake_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Front door lights",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="evening-lights-off",
        label="Evening lights off",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DEVICE_STATE, RuleTrigger.DWELL_SATISFIED],
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
        triggers=[RuleTrigger.SCHEDULED],
    )


def test_extract_astronomical_anchor_returns_single_top_level_anchor() -> None:
    anchor = extract_astronomical_anchor(_scheduled_rule(schedule_cron=None))
    assert anchor is not None
    assert anchor.condition_type == "after_sunset"
    assert anchor.offset_minutes == -15


def test_uses_astronomical_schedule_when_anchor_without_cron() -> None:
    assert uses_astronomical_schedule(_scheduled_rule(schedule_cron=None)) is True


def test_uses_astronomical_schedule_when_anchor_with_repeat_cron() -> None:
    assert uses_astronomical_schedule(_scheduled_rule(schedule_cron="*/10 * * * *")) is True


def test_uses_astronomical_repeat_schedule_when_anchor_with_repeat_cron() -> None:
    assert (
        uses_astronomical_repeat_schedule(
            _scheduled_rule(schedule_cron="*/10 * * * *"),
        )
        is True
    )


def test_uses_astronomical_repeat_schedule_false_without_cron() -> None:
    assert uses_astronomical_repeat_schedule(_scheduled_rule(schedule_cron=None)) is False


def test_uses_astronomical_schedule_for_edge_and_scheduled_window_open_rule() -> None:
    assert uses_astronomical_schedule(_edge_window_open_rule()) is True


def test_uses_astronomical_edge_window_open_schedule_when_opted_in() -> None:
    assert uses_astronomical_edge_window_open_schedule(_edge_window_open_rule()) is True


def test_uses_astronomical_edge_window_open_schedule_false_for_enter_only_evening() -> None:
    enter_only = _edge_window_open_rule().model_copy(update={"triggers": [RuleTrigger.EDGE_TRUE]})
    assert uses_astronomical_edge_window_open_schedule(enter_only) is False


def test_uses_astronomical_edge_window_open_schedule_false_with_repeat_cron() -> None:
    with_repeat = _edge_window_open_rule().model_copy(
        update={"schedule_cron": "*/10 * * * *"},
    )
    assert uses_astronomical_edge_window_open_schedule(with_repeat) is False


def test_uses_astronomical_eligibility_wake_false_when_scheduled_present() -> None:
    with_scheduled = _eligibility_wake_rule().model_copy(
        update={"triggers": [RuleTrigger.DWELL_SATISFIED, RuleTrigger.SCHEDULED]},
    )
    assert uses_astronomical_eligibility_wake(with_scheduled) is False


def test_uses_astronomical_eligibility_wake_false_with_repeat_cron() -> None:
    with_repeat = _eligibility_wake_rule().model_copy(
        update={"schedule_cron": "*/10 * * * *"},
    )
    assert uses_astronomical_eligibility_wake(with_repeat) is False


def test_uses_astronomical_eligibility_wake_for_dwell_and_device_state() -> None:
    assert uses_astronomical_eligibility_wake(_eligibility_wake_rule()) is True


def test_uses_astronomical_materialized_schedule_includes_eligibility_wake() -> None:
    assert uses_astronomical_materialized_schedule(_eligibility_wake_rule()) is True
    assert (
        uses_astronomical_materialized_schedule(
            _scheduled_rule(schedule_cron=None),
        )
        is True
    )


def test_uses_astronomical_schedule_false_when_scheduled_trigger_missing() -> None:
    enter_only = _edge_window_open_rule().model_copy(update={"triggers": [RuleTrigger.EDGE_TRUE]})
    assert uses_astronomical_schedule(enter_only) is False


def test_rule_out_rejects_repeat_cron_with_dual_edge_and_scheduled_triggers() -> None:
    with pytest.raises(ValueError, match="do not allow schedule_cron"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    AfterSunsetCondition(
                        type="after_sunset",
                        offset_minutes=-25,
                        window_end="midnight",
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            fire_once_per_local_day=True,
            id="dual-trigger-repeat",
            label="Dual trigger repeat",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            schedule_cron="*/10 * * * *",
            triggers=[RuleTrigger.EDGE_TRUE, RuleTrigger.SCHEDULED],
        )


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


def test_materialize_astronomical_cron_returns_repeat_cron_when_present() -> None:
    tz = ZoneInfo("America/New_York")
    sun = RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )
    cron = materialize_astronomical_cron(
        _scheduled_rule(schedule_cron="*/10 * * * *"),
        sun=sun,
        timezone=tz,
    )
    assert cron == "*/10 * * * *"


def test_next_astronomical_repeat_evaluate_at_waits_for_anchor_before_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tz = ZoneInfo("America/New_York")
    settings = SettingsLocationOut(
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
        home_label="Home",
    )
    sun = RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )
    monkeypatch.setattr(
        "app.rule_conditions.compute_rules_sun_out",
        lambda *args, **kwargs: sun,
    )
    anchor = extract_astronomical_anchor(_scheduled_rule(schedule_cron="*/10 * * * *"))
    assert anchor is not None
    anchor_dt = astronomical_anchor_datetime(anchor, sun, tz)
    before_anchor = anchor_dt - timedelta(minutes=5)
    next_at = next_astronomical_repeat_evaluate_at(
        _scheduled_rule(schedule_cron="*/10 * * * *"),
        settings=settings,
        timezone=tz,
        now=before_anchor,
    )
    assert next_at == anchor_dt.timestamp()


def test_next_astronomical_repeat_evaluate_at_after_before_sunrise_window_returns_midnight() -> None:
    tz = ZoneInfo("America/New_York")
    settings = SettingsLocationOut(
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
        home_label="Home",
    )
    rule = _before_sunrise_rule(schedule_cron="*/10 * * * *")
    anchor = extract_astronomical_anchor(rule)
    assert anchor is not None
    local_noon = datetime(2023, 11, 14, 12, 0, tzinfo=tz)
    sun = compute_rules_sun_out(settings, now=local_noon)
    anchor_dt = astronomical_anchor_datetime(anchor, sun, tz)
    after_window = anchor_dt + timedelta(minutes=15)
    next_at = next_astronomical_repeat_evaluate_at(
        rule,
        settings=settings,
        timezone=tz,
        now=after_window,
    )
    expected_midnight = datetime.combine(
        after_window.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=tz,
    )
    assert next_at == expected_midnight.timestamp()


def test_next_astronomical_repeat_evaluate_at_polls_after_anchor() -> None:
    tz = ZoneInfo("America/New_York")
    settings = SettingsLocationOut(
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
        home_label="Home",
    )
    sun = RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )
    anchor = extract_astronomical_anchor(_scheduled_rule(schedule_cron="*/10 * * * *"))
    assert anchor is not None
    anchor_dt = astronomical_anchor_datetime(anchor, sun, tz)
    after_anchor = anchor_dt + timedelta(minutes=3)
    next_at = next_astronomical_repeat_evaluate_at(
        _scheduled_rule(schedule_cron="*/10 * * * *"),
        settings=settings,
        timezone=tz,
        now=after_anchor,
    )
    from croniter import croniter

    expected = croniter("*/10 * * * *", anchor_dt).get_next(datetime)
    if expected.tzinfo is None:
        expected = expected.replace(tzinfo=tz)
    assert next_at == expected.timestamp()


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
            triggers=[RuleTrigger.SCHEDULED],
        )
