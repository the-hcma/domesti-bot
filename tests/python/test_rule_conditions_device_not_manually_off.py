"""Unit tests for the device_not_manually_off_within condition."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    DeviceNotManuallyOffWithinCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceFamilyId, RuleTrigger
from app.rule_conditions import (
    NOT_MANUALLY_OFF_ELAPSED_DETAIL,
    NOT_MANUALLY_OFF_NEVER_DETAIL,
    NOT_MANUALLY_OFF_WAITING_DETAIL,
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rule_validation import build_roster_user_id_lookup

_LABEL = "Master bedroom light (02:00:00:00:00:08)"
_MAC = "02:00:00:00:00:08"
_NOW = datetime(2026, 10, 14, 19, 0, tzinfo=ZoneInfo("America/New_York"))
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)


def test_condition_met_when_device_was_never_turned_off_by_hand() -> None:
    result = evaluate_rule(_rule(), _ctx())
    assert result.all_met is True
    assert result.conditions[0].detail == NOT_MANUALLY_OFF_NEVER_DETAIL.format(device=_LABEL)


def test_condition_blocks_inside_window_and_reports_time_left() -> None:
    result = evaluate_rule(_rule(window_s=3600), _ctx(off_ago_s=22 * 60))
    assert result.all_met is False
    assert result.conditions[0].detail == NOT_MANUALLY_OFF_WAITING_DETAIL.format(
        device=_LABEL,
        elapsed="22 min",
        remaining="38 min",
    )


def test_condition_met_once_window_has_elapsed() -> None:
    result = evaluate_rule(_rule(window_s=3600), _ctx(off_ago_s=3600))
    assert result.all_met is True
    assert result.conditions[0].detail == NOT_MANUALLY_OFF_ELAPSED_DETAIL.format(
        device=_LABEL,
        elapsed="60 min",
        window="60 min",
    )


def test_condition_ignores_manual_off_of_another_device() -> None:
    ctx = _ctx(off_ago_s=60, off_device_id="02:00:00:00:00:99")
    assert evaluate_rule(_rule(), ctx).all_met is True


def test_condition_rejects_non_kasa_device_family() -> None:
    with pytest.raises(ValidationError) as excinfo:
        DeviceNotManuallyOffWithinCondition(
            type="device_not_manually_off_within",
            device=RuleConditionDeviceRefOut(device_id="door-left", family_id=DeviceFamilyId.TAILWIND),
            window_s=3600,
        )
    assert "device_not_manually_off_within" in str(excinfo.value)


def test_condition_rejects_non_positive_window() -> None:
    with pytest.raises(ValidationError):
        DeviceNotManuallyOffWithinCondition(
            type="device_not_manually_off_within",
            device=_ref(),
            window_s=0,
        )


def _ctx(
    *,
    off_ago_s: float | None = None,
    off_device_id: str = _MAC,
) -> RuleEvaluationContext:
    user_display_names = {"henrique": "Henrique"}
    manual_off: dict[tuple[DeviceFamilyId, str], float] = {}
    if off_ago_s is not None:
        manual_off[(DeviceFamilyId.KASA, off_device_id)] = _NOW.timestamp() - off_ago_s
    return RuleEvaluationContext(
        geofences=(),
        now=_NOW,
        roster_user_id_lookup=build_roster_user_id_lookup(list(user_display_names)),
        sun=compute_rules_sun_out(_SETTINGS, now=_NOW),
        timezone=ZoneInfo("America/New_York"),
        user_display_names=user_display_names,
        user_locations={},
        device_manual_off_at=manual_off,
    )


def _ref() -> RuleConditionDeviceRefOut:
    return RuleConditionDeviceRefOut(
        device_id=_MAC,
        display_name="Master bedroom light",
        family_id=DeviceFamilyId.KASA,
    )


def _rule(*, window_s: int = 3600) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DeviceNotManuallyOffWithinCondition(
                    type="device_not_manually_off_within",
                    device=_ref(),
                    window_s=window_s,
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="master-bedroom-light-on",
        label="Master bedroom light on",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="0 19 * * *",
    )
