"""Unit tests for devices_any_in_state_for_s condition evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.api.schemas import (
    DevicesAnyInStateForSCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, evaluate_rule
from app.rule_validation import build_roster_user_id_lookup

def test_devices_any_in_state_for_s_met_when_open_long_enough() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    since = now.timestamp() - 1300.0
    state = _tailwind_state(_FakeTailwindDoor("door-left", "Left", is_open=True))
    result = evaluate_rule(
        _open_for_s_rule(),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.TAILWIND, "door-left"): since},
        ),
    )
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Open: Left" in result.conditions[0].detail


def test_devices_any_in_state_for_s_pending_when_open_too_recent() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    since = now.timestamp() - 60.0
    state = _tailwind_state(_FakeTailwindDoor("door-left", "Left", is_open=True))
    result = evaluate_rule(
        _open_for_s_rule(),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.TAILWIND, "door-left"): since},
        ),
    )
    assert result.all_met is False
    assert result.conditions[0].met is False
    assert "need" in result.conditions[0].detail


def test_devices_any_in_state_for_s_unmet_when_door_closed() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    since = now.timestamp() - 1300.0
    state = _tailwind_state(_FakeTailwindDoor("door-left", "Left", is_open=False))
    result = evaluate_rule(
        _open_for_s_rule(),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.TAILWIND, "door-left"): since},
        ),
    )
    assert result.all_met is False
    assert result.conditions[0].met is False
    assert "not open" in result.conditions[0].detail


def test_devices_any_in_state_for_s_unmet_when_discovery_not_ready() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    result = evaluate_rule(_open_for_s_rule(), _ctx(now=now, device_state=None))
    assert result.all_met is False
    assert result.conditions[0].met is False
    assert "discovery not ready" in result.conditions[0].detail


_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_open = is_open


def _ctx(
    *,
    now: datetime,
    device_state: DeviceManagersState | None = None,
    device_bool_since: dict[tuple[DeviceFamilyId, str], float] | None = None,
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
        device_bool_since=device_bool_since or {},
    )


def _open_for_s_rule(*, min_duration_s: int = 1200) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateForSCondition(
                    type="devices_any_in_state_for_s",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Left",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                    min_duration_s=min_duration_s,
                    state=DeviceConditionState.OPEN,
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="door-open-dwell",
        label="Door open dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _tailwind_state(*doors: _FakeTailwindDoor) -> DeviceManagersState:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=mgr,
        vizio_mgr=None,
    )
