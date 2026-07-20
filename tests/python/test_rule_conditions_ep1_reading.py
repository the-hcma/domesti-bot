"""Unit tests for ep1_reading_compare conditions."""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    Ep1ReadingCompareCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_display import format_device_display
from app.device_enums import (
    DeviceFamilyId,
    Ep1ReadingComparison,
    Ep1ReadingMetric,
    RuleTrigger,
)
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, evaluate_rule
from app.rule_validation import (
    RuleValidationContext,
    build_roster_user_id_lookup,
    validate_rule,
)


def test_ep1_reading_compare_met_when_humidity_below_threshold() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _ep1_state(_FakeEp1Sensor(_MAC, "Office EP1", humidity_pct=30.0))
    result = evaluate_rule(
        _reading_rule(
            comparison=Ep1ReadingComparison.BELOW,
            metric=Ep1ReadingMetric.HUMIDITY_PCT,
            threshold=40.0,
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "30%" in result.conditions[0].detail
    assert "below 40%" in result.conditions[0].detail


def test_ep1_reading_compare_met_when_temperature_above_threshold() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _ep1_state(_FakeEp1Sensor(_MAC, "Office EP1", temperature_c=25.5))
    result = evaluate_rule(
        _reading_rule(
            comparison=Ep1ReadingComparison.ABOVE,
            metric=Ep1ReadingMetric.TEMPERATURE_C,
            threshold=24.0,
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    detail = result.conditions[0].detail
    assert format_device_display(_MAC, "Office EP1") in detail
    assert "25.5°C" in detail
    assert "above 24°C" in detail


def test_ep1_reading_compare_rejects_non_ep1_family() -> None:
    with pytest.raises(ValidationError, match="family_id"):
        Ep1ReadingCompareCondition(
            type="ep1_reading_compare",
            comparison=Ep1ReadingComparison.ABOVE,
            metric=Ep1ReadingMetric.TEMPERATURE_C,
            threshold=24.0,
            device=RuleConditionDeviceRefOut(
                device_id=_MAC,
                family_id=DeviceFamilyId.KASA,
            ),
        )


def test_ep1_reading_compare_unmet_when_reading_unavailable() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _ep1_state(_FakeEp1Sensor(_MAC, "Office EP1", temperature_c=None))
    result = evaluate_rule(
        _reading_rule(
            comparison=Ep1ReadingComparison.ABOVE,
            metric=Ep1ReadingMetric.TEMPERATURE_C,
            threshold=24.0,
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is False
    assert "reading unavailable" in result.conditions[0].detail


def test_ep1_reading_compare_unmet_when_temperature_not_above() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _ep1_state(_FakeEp1Sensor(_MAC, "Office EP1", temperature_c=22.0))
    result = evaluate_rule(
        _reading_rule(
            comparison=Ep1ReadingComparison.ABOVE,
            metric=Ep1ReadingMetric.TEMPERATURE_C,
            threshold=24.0,
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is False
    assert "not above" in result.conditions[0].detail


def test_validate_rule_accepts_ep1_reading_compare() -> None:
    rule = _reading_rule(
        comparison=Ep1ReadingComparison.ABOVE,
        metric=Ep1ReadingMetric.ILLUMINANCE_LX,
        threshold=100.0,
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with (
        patch(
            "app.rule_validation.resolve_ep1_identifier_by_label",
            return_value=_MAC,
        ),
        patch(
            "app.rule_validation.lookup_preferred_label",
            return_value="Office EP1",
        ),
    ):
        issues = validate_rule(rule, ctx)
    assert issues == []


class _FakeEp1Sensor:
    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        temperature_c: float | None = None,
        humidity_pct: float | None = None,
        illuminance_lx: float | None = None,
    ) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.illuminance_lx = illuminance_lx


_MAC = "02:00:00:00:00:20"

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)

_TZ = ZoneInfo("America/New_York")


def _ctx(
    *,
    now: datetime,
    device_state: DeviceManagersState | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique", "kristen": "Kristen"}
    return RuleEvaluationContext(
        geofences=(),
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(
            list(user_display_names.keys()),
        ),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations={},
        device_state=device_state,
        device_bool_since={},
    )


def _ep1_state(*sensors: _FakeEp1Sensor) -> DeviceManagersState:
    mgr = MagicMock(spec=Ep1DeviceManager)
    mgr.devices = tuple(sensors)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=mgr,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _reading_rule(
    *,
    comparison: Ep1ReadingComparison,
    metric: Ep1ReadingMetric,
    threshold: float,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                Ep1ReadingCompareCondition(
                    type="ep1_reading_compare",
                    comparison=comparison,
                    metric=metric,
                    threshold=threshold,
                    device=RuleConditionDeviceRefOut(
                        device_id=_MAC,
                        display_name="Office EP1",
                        family_id=DeviceFamilyId.EP1,
                    ),
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="ep1-reading",
        label="EP1 reading",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
