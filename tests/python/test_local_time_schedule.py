"""Unit tests for local_time_window eligibility schedule helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AfterSunsetCondition,
    AllConditionsCondition,
    Ep1ReadingCompareCondition,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import (
    DeviceFamilyId,
    Ep1ReadingComparison,
    Ep1ReadingMetric,
    RuleTrigger,
)
from app.local_time_schedule import (
    extract_top_level_local_time_window,
    is_local_time_window_open,
    local_time_window_start_datetime,
    materialize_local_time_window_cron,
    uses_local_time_window_eligibility_wake,
    uses_local_time_window_materialized_schedule,
)


def test_extract_top_level_local_time_window_single() -> None:
    rule = _eligibility_rule()
    window = extract_top_level_local_time_window(rule)
    assert window is not None
    assert window.start_hhmm == "21:00"
    assert window.end_hhmm == "00:00"


def test_local_time_window_rejects_invalid_hhmm() -> None:
    with pytest.raises(ValidationError, match="HH:MM"):
        LocalTimeWindowCondition(
            type="local_time_window",
            start_hhmm="25:00",
            end_hhmm="00:00",
        )
    with pytest.raises(ValidationError, match="HH:MM"):
        LocalTimeWindowCondition(
            type="local_time_window",
            start_hhmm="21:00",
            end_hhmm="not-a-time",
        )


def test_local_time_window_start_datetime_builds_today() -> None:
    window = LocalTimeWindowCondition(
        type="local_time_window",
        start_hhmm="21:00",
        end_hhmm="00:00",
    )
    tz = ZoneInfo("America/New_York")
    start = local_time_window_start_datetime(
        window,
        local_date=datetime(2023, 11, 14, tzinfo=tz).date(),
        timezone=tz,
    )
    assert start == datetime(2023, 11, 14, 21, 0, tzinfo=tz)


def test_materialize_local_time_window_cron() -> None:
    rule = _eligibility_rule()
    tz = ZoneInfo("America/New_York")
    now = datetime(2023, 11, 14, 18, 0, tzinfo=tz)
    cron = materialize_local_time_window_cron(rule, timezone=tz, now=now)
    assert cron == "0 21 * * *"


def test_is_local_time_window_open_overnight() -> None:
    window = LocalTimeWindowCondition(
        type="local_time_window",
        start_hhmm="21:00",
        end_hhmm="00:00",
    )
    tz = ZoneInfo("America/New_York")
    assert is_local_time_window_open(window, now=datetime(2023, 11, 14, 21, 30, tzinfo=tz)) is True
    assert is_local_time_window_open(window, now=datetime(2023, 11, 14, 20, 59, tzinfo=tz)) is False
    assert is_local_time_window_open(window, now=datetime(2023, 11, 15, 0, 0, tzinfo=tz)) is False


def test_rule_rejects_astronomical_and_local_time_window_eligibility() -> None:
    with pytest.raises(ValidationError, match="at most one eligibility window"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    AfterSunsetCondition(type="after_sunset", offset_minutes=0),
                    LocalTimeWindowCondition(
                        type="local_time_window",
                        start_hhmm="21:00",
                        end_hhmm="00:00",
                    ),
                    Ep1ReadingCompareCondition(
                        type="ep1_reading_compare",
                        comparison=Ep1ReadingComparison.BELOW,
                        metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                        threshold=34.0,
                        device=RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:01",
                            family_id=DeviceFamilyId.EP1,
                        ),
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="dual-eligibility",
            label="Dual eligibility",
            min_location_accuracy_m=50,
            notification_emails=["ops@example.com"],
            notify_on_fire=True,
            triggers=[RuleTrigger.DEVICE_STATE],
        )


def test_rule_rejects_multiple_top_level_local_time_windows() -> None:
    with pytest.raises(ValidationError, match="at most one top-level local_time_window"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    LocalTimeWindowCondition(
                        type="local_time_window",
                        start_hhmm="18:00",
                        end_hhmm="20:00",
                    ),
                    LocalTimeWindowCondition(
                        type="local_time_window",
                        start_hhmm="21:00",
                        end_hhmm="00:00",
                    ),
                    Ep1ReadingCompareCondition(
                        type="ep1_reading_compare",
                        comparison=Ep1ReadingComparison.BELOW,
                        metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                        threshold=34.0,
                        device=RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:01",
                            family_id=DeviceFamilyId.EP1,
                        ),
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="multi-window",
            label="Multi window",
            min_location_accuracy_m=50,
            notification_emails=["ops@example.com"],
            notify_on_fire=True,
            triggers=[RuleTrigger.DEVICE_STATE],
        )


def test_rule_rejects_nested_local_time_window() -> None:
    with pytest.raises(ValidationError, match="top-level conditions.all"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    AllConditionsCondition(
                        type="all",
                        conditions=[
                            LocalTimeWindowCondition(
                                type="local_time_window",
                                start_hhmm="21:00",
                                end_hhmm="00:00",
                            ),
                        ],
                    ),
                    Ep1ReadingCompareCondition(
                        type="ep1_reading_compare",
                        comparison=Ep1ReadingComparison.BELOW,
                        metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                        threshold=34.0,
                        device=RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:01",
                            family_id=DeviceFamilyId.EP1,
                        ),
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="nested-window",
            label="Nested window",
            min_location_accuracy_m=50,
            notification_emails=["ops@example.com"],
            notify_on_fire=True,
            triggers=[RuleTrigger.DEVICE_STATE],
        )


def test_uses_local_time_window_eligibility_wake_for_device_state() -> None:
    assert uses_local_time_window_eligibility_wake(_eligibility_rule()) is True
    assert uses_local_time_window_materialized_schedule(_eligibility_rule()) is True


def test_uses_local_time_window_eligibility_wake_false_when_scheduled() -> None:
    with_scheduled = _eligibility_rule().model_copy(
        update={"triggers": [RuleTrigger.DEVICE_STATE, RuleTrigger.SCHEDULED]},
    )
    assert uses_local_time_window_eligibility_wake(with_scheduled) is False


def test_uses_local_time_window_eligibility_wake_false_with_cron() -> None:
    with_cron = _eligibility_rule().model_copy(update={"schedule_cron": "0 21 * * *"})
    assert uses_local_time_window_eligibility_wake(with_cron) is False


def _eligibility_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                LocalTimeWindowCondition(
                    type="local_time_window",
                    start_hhmm="21:00",
                    end_hhmm="00:00",
                ),
                Ep1ReadingCompareCondition(
                    type="ep1_reading_compare",
                    comparison=Ep1ReadingComparison.BELOW,
                    metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                    threshold=34.0,
                    device=RuleConditionDeviceRefOut(
                        device_id="aa:bb:cc:dd:ee:01",
                        family_id=DeviceFamilyId.EP1,
                    ),
                ),
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="evening-window-eligibility",
        label="Evening window eligibility",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
    )
