"""Unit tests for ``rule_eligible_since`` — the clamp instant for pre-eligibility signal."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterLocalTimeCondition,
    AfterSunsetCondition,
    BeforeLocalTimeCondition,
    DevicesAnyInStateForSCondition,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, rule_eligible_since
from app.rule_validation import build_roster_user_id_lookup

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def test_rule_eligible_since_none_when_no_temporal_gate() -> None:
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions([])
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_none_when_after_local_time_not_yet_open() -> None:
    now = datetime(2026, 9, 2, 20, 59, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00")],
    )
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_after_local_time_opens_today() -> None:
    now = datetime(2026, 9, 2, 21, 10, 13, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00")],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_same_day_start() -> None:
    now = datetime(2026, 9, 2, 9, 30, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="10:00",
                start_hhmm="09:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 9, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_overnight_wrap_tail_opened_yesterday() -> None:
    # Window is 22:00-06:00 (wraps past midnight); "now" is in the early-morning
    # tail, so the window that is currently open actually opened yesterday.
    now = datetime(2026, 9, 3, 1, 30, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="06:00",
                start_hhmm="22:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 22, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_overnight_wrap_evening_portion_opened_today() -> None:
    now = datetime(2026, 9, 2, 23, 15, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="06:00",
                start_hhmm="22:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 22, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_after_sunset_opens_at_sunset_plus_offset() -> None:
    now = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ)
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    sunset_at = datetime.fromisoformat(sun.sunset_at.replace("Z", "+00:00")).astimezone(_TZ)
    rule = _rule_with_conditions([AfterSunsetCondition(type="after_sunset", offset_minutes=15)])
    expected = (sunset_at.replace(second=0, microsecond=0) + timedelta(minutes=15)).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_ignores_before_local_time() -> None:
    # ``before_*`` gates are open starting at local midnight, so they never
    # raise the eligibility instant above "no clamp".
    now = datetime(2026, 9, 2, 8, 0, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [BeforeLocalTimeCondition(type="before_local_time", time_hhmm="10:00")],
    )
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_returns_latest_of_multiple_gates() -> None:
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="23:00",
                start_hhmm="20:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def _ctx(*, now: datetime) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique"}
    return RuleEvaluationContext(
        geofences=(),
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(list(user_display_names.keys())),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations={},
    )


def _dwell_condition() -> DevicesAnyInStateForSCondition:
    return DevicesAnyInStateForSCondition(
        type="devices_any_in_state_for_s",
        devices=[
            RuleConditionDeviceRefOut(
                device_id="28:05:a5:28:c8:48",
                family_id=DeviceFamilyId.EP1,
            ),
        ],
        min_duration_s=10,
        state=DeviceConditionState.CLEAR,
    )


def _rule_with_conditions(conditions: list[RuleConditionOut]) -> RuleOut:
    # dwell_satisfied rules require a *_for_s / devices_any_in_state_for_s
    # condition; append one so temporal-gate-only condition lists stay valid.
    # rule_eligible_since ignores it — it only inspects the temporal gates.
    return RuleOut(
        conditions=RuleConditionsOut(all=[*conditions, _dwell_condition()]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="eligibility-test-rule",
        label="Eligibility test rule",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )
