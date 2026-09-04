"""Unit tests for devices_any_in_state_for_s condition evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterLocalTimeCondition,
    DevicesAnyInStateForSCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import (
    RuleEvaluationContext,
    _format_dwell_elapsed_s,
    compute_rules_sun_out,
    evaluate_rule,
    natural_bool_for_device_family,
)
from app.rule_validation import build_roster_user_id_lookup


def test_devices_any_in_state_for_s_met_when_ep1_occupied_long_enough() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    since = now.timestamp() - 1300.0
    mac = "02:00:00:00:00:20"
    state = _ep1_state(_FakeEp1Sensor(mac, "Office EP1", occupied=True))
    result = evaluate_rule(
        _occupied_for_s_rule(device_id=mac),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.EP1, mac): since},
        ),
    )
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Occupied:" in result.conditions[0].detail
    assert "Office EP1" in result.conditions[0].detail


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


def test_devices_any_in_state_for_s_pending_when_time_gate_just_opened() -> None:
    # EP1 has been clear for hours, well before the 21:00 after_local_time gate
    # opened — but the gate itself only opened 1s ago, so "clear for 10s after
    # 9pm" is not yet satisfied. The raw pre-gate streak must not count.
    now = datetime(2026, 9, 2, 21, 0, 1, tzinfo=_TZ)
    since = now.timestamp() - 7200.0
    mac = "28:05:a5:28:c8:48"
    state = _ep1_state(_FakeEp1Sensor(mac, "Master bedroom EP1", occupied=False))
    result = evaluate_rule(
        _clear_for_s_after_local_time_rule(device_id=mac, min_duration_s=10, time_hhmm="21:00"),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.EP1, mac): since},
        ),
    )
    assert result.all_met is False
    dwell_condition = next(row for row in result.conditions if "Any device" in row.label)
    assert dwell_condition.met is False
    assert "need" in dwell_condition.detail


def test_devices_any_in_state_for_s_met_once_gate_open_for_min_duration() -> None:
    now = datetime(2026, 9, 2, 21, 0, 10, tzinfo=_TZ)
    since = now.timestamp() - 7200.0
    mac = "28:05:a5:28:c8:48"
    state = _ep1_state(_FakeEp1Sensor(mac, "Master bedroom EP1", occupied=False))
    result = evaluate_rule(
        _clear_for_s_after_local_time_rule(device_id=mac, min_duration_s=10, time_hhmm="21:00"),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.EP1, mac): since},
        ),
    )
    assert result.all_met is True
    dwell_condition = next(row for row in result.conditions if "Any device" in row.label)
    assert dwell_condition.met is True
    # Elapsed is reported from the gate opening (10s), not the raw 2h streak.
    assert _format_dwell_elapsed_s(10.0) in dwell_condition.detail


def test_devices_any_in_state_for_s_unclamped_when_streak_starts_after_gate() -> None:
    # Streak began comfortably after the gate opened — the clamp is a no-op.
    now = datetime(2026, 9, 2, 21, 30, 0, tzinfo=_TZ)
    since = now.timestamp() - 1200.0
    mac = "28:05:a5:28:c8:48"
    state = _ep1_state(_FakeEp1Sensor(mac, "Master bedroom EP1", occupied=False))
    result = evaluate_rule(
        _clear_for_s_after_local_time_rule(device_id=mac, min_duration_s=10, time_hhmm="21:00"),
        _ctx(
            now=now,
            device_state=state,
            device_bool_since={(DeviceFamilyId.EP1, mac): since},
        ),
    )
    assert result.all_met is True


def test_natural_bool_for_ep1_occupied_and_clear() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    mac = "02:00:00:00:00:20"
    occupied_state = _ep1_state(_FakeEp1Sensor(mac, "Office EP1", occupied=True))
    clear_state = _ep1_state(_FakeEp1Sensor(mac, "Office EP1", occupied=False))
    assert (
        natural_bool_for_device_family(
            _ctx(now=now, device_state=occupied_state),
            family_id=DeviceFamilyId.EP1,
            device_id=mac,
        )
        is True
    )
    assert (
        natural_bool_for_device_family(
            _ctx(now=now, device_state=clear_state),
            family_id=DeviceFamilyId.EP1,
            device_id=mac,
        )
        is False
    )


def test_natural_bool_for_ep1_unknown_returns_none() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    mac = "02:00:00:00:00:20"
    state = _ep1_state(_FakeEp1Sensor(mac, "Office EP1", occupied=None))
    assert (
        natural_bool_for_device_family(
            _ctx(now=now, device_state=state),
            family_id=DeviceFamilyId.EP1,
            device_id=mac,
        )
        is None
    )


_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


class _FakeEp1Sensor:
    def __init__(self, identifier: str, label: str, *, occupied: bool | None) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self._occupancy_bool = occupied

    @property
    def occupancy_state(self) -> str:
        if self._occupancy_bool is True:
            return DeviceConditionState.OCCUPIED.value
        if self._occupancy_bool is False:
            return DeviceConditionState.CLEAR.value
        return "unknown"


class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.mac_address = None
        self.door_key = self.identifier
        self.preferred_label = label
        self.is_open = is_open


def _clear_for_s_after_local_time_rule(
    *,
    device_id: str,
    min_duration_s: int,
    time_hhmm: str,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterLocalTimeCondition(type="after_local_time", time_hhmm=time_hhmm),
                DevicesAnyInStateForSCondition(
                    type="devices_any_in_state_for_s",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id=device_id,
                            display_name="Master bedroom EP1",
                            family_id=DeviceFamilyId.EP1,
                        ),
                    ],
                    min_duration_s=min_duration_s,
                    state=DeviceConditionState.CLEAR,
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="evening-ep1-clear-master-bedroom-lamp-on",
        label="Turn on Master bedroom lamp when EP1 is clear after 9pm",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


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


def _occupied_for_s_rule(*, device_id: str, min_duration_s: int = 1200) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateForSCondition(
                    type="devices_any_in_state_for_s",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id=device_id,
                            display_name="Office EP1",
                            family_id=DeviceFamilyId.EP1,
                        ),
                    ],
                    min_duration_s=min_duration_s,
                    state=DeviceConditionState.OCCUPIED,
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="ep1-occupied-dwell",
        label="EP1 occupied dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
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
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=mgr,
        vizio_mgr=None,
    )
