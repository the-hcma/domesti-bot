"""Unit tests for devices_any_in_state / devices_all_in_state conditions."""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest
from pydantic import TypeAdapter

from app.api.schemas import (
    DevicesAllInStateCondition,
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, evaluate_rule
from app.rule_validation import (
    RuleValidationContext,
    build_roster_user_id_lookup,
    validate_rule,
)
from app.sonos_device_manager import SonosDeviceManager

_CONDITION_ADAPTER = TypeAdapter(RuleConditionOut)
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def _load_condition(data: dict[str, object]) -> RuleConditionOut:
    """Validate a condition via ``RuleConditionsOut`` (load-time legacy aliases)."""
    return RuleConditionsOut.model_validate({"all": [data]}).all[0]


def test_devices_all_in_state_met_when_every_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=True),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAllInStateCondition(
                type="devices_all_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                    RuleConditionDeviceRefOut(
                        device_id="Garage outside lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.ON,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "All on" in result.conditions[0].detail


def test_devices_all_in_state_unmet_when_one_switch_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAllInStateCondition(
                type="devices_all_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                    RuleConditionDeviceRefOut(
                        device_id="Garage outside lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.ON,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.conditions[0].met is False
    assert "Off: Garage outside lights" in result.conditions[0].detail


def test_devices_all_in_state_met_when_every_door_closed() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _tailwind_device_state(
        _FakeTailwindDoor("door-left", "Left", is_open=False),
        _FakeTailwindDoor("door-right", "Right", is_open=False),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAllInStateCondition(
                type="devices_all_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Left",
                        family_id=DeviceFamilyId.TAILWIND,
                    ),
                    RuleConditionDeviceRefOut(
                        device_id="Right",
                        family_id=DeviceFamilyId.TAILWIND,
                    ),
                ],
                state=DeviceConditionState.CLOSED,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "All closed" in result.conditions[0].detail


def test_devices_any_in_state_met_when_one_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                    RuleConditionDeviceRefOut(
                        device_id="Garage outside lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.ON,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "On: Front door lights" in result.conditions[0].detail


def test_devices_any_in_state_met_when_door_open() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _tailwind_device_state(
        _FakeTailwindDoor("door-left", "Left", is_open=True),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Left",
                        family_id=DeviceFamilyId.TAILWIND,
                    ),
                ],
                state=DeviceConditionState.OPEN,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "Open: Left" in result.conditions[0].detail


def test_devices_any_in_state_unmet_when_all_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=False),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.ON,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.conditions[0].met is False
    assert "All off" in result.conditions[0].detail


def test_devices_any_in_state_unmet_when_discovery_not_ready() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.ON,
            ),
        ),
        _ctx(now=now, device_state=None),
    )
    assert result.conditions[0].met is False
    assert "discovery not ready" in result.conditions[0].detail


def test_devices_any_in_state_reports_unsupported_family() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Front door lights",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
                state=DeviceConditionState.OPEN,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.conditions[0].met is False
    assert "unsupported family kasa" in result.conditions[0].detail


def test_legacy_devices_any_on_type_migrates_to_any_in_state() -> None:
    loaded = _load_condition(
        {
            "type": "devices_any_on",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert isinstance(loaded, DevicesAnyInStateCondition)
    assert loaded.type == "devices_any_in_state"
    assert loaded.state == DeviceConditionState.ON
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "On: Front door lights" in result.conditions[0].detail


def test_legacy_devices_all_on_type_migrates_to_all_in_state() -> None:
    loaded = _load_condition(
        {
            "type": "devices_all_on",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert isinstance(loaded, DevicesAllInStateCondition)
    assert loaded.type == "devices_all_in_state"
    assert loaded.state == DeviceConditionState.ON
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "All on" in result.conditions[0].detail


def test_legacy_devices_any_off_and_open_types_migrate() -> None:
    off_loaded = _load_condition(
        {
            "type": "devices_any_off",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert isinstance(off_loaded, DevicesAnyInStateCondition)
    assert off_loaded.state == DeviceConditionState.OFF

    open_loaded = _load_condition(
        {
            "type": "devices_any_open",
            "devices": [
                {"family_id": "tailwind", "device_id": "Left"},
            ],
        },
    )
    assert isinstance(open_loaded, DevicesAnyInStateCondition)
    assert open_loaded.state == DeviceConditionState.OPEN


def test_legacy_device_types_migrate_inside_nested_all() -> None:
    loaded = RuleConditionsOut.model_validate(
        {
            "all": [
                {
                    "type": "all",
                    "conditions": [
                        {
                            "type": "devices_any_on",
                            "devices": [
                                {"family_id": "kasa", "device_id": "Front door lights"},
                            ],
                        },
                    ],
                },
            ],
        },
    )
    nested = loaded.all[0]
    assert nested.type == "all"
    child = nested.conditions[0]
    assert isinstance(child, DevicesAnyInStateCondition)
    assert child.type == "devices_any_in_state"
    assert child.state == DeviceConditionState.ON


def test_devices_any_in_state_on_loads_and_evaluates() -> None:
    loaded = _CONDITION_ADAPTER.validate_python(
        {
            "type": "devices_any_in_state",
            "state": "on",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert loaded.type == "devices_any_in_state"
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "On: Front door lights" in result.conditions[0].detail


def test_devices_all_in_state_on_loads_and_evaluates() -> None:
    loaded = _CONDITION_ADAPTER.validate_python(
        {
            "type": "devices_all_in_state",
            "state": "on",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert loaded.type == "devices_all_in_state"
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "All on" in result.conditions[0].detail


def test_devices_any_in_state_off_loads_and_evaluates() -> None:
    loaded = _CONDITION_ADAPTER.validate_python(
        {
            "type": "devices_any_in_state",
            "state": "off",
            "devices": [
                {"family_id": "kasa", "device_id": "Front door lights"},
            ],
        },
    )
    assert loaded.type == "devices_any_in_state"
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=False),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "Off: Front door lights" in result.conditions[0].detail


def test_devices_any_in_state_open_loads_and_evaluates() -> None:
    loaded = _CONDITION_ADAPTER.validate_python(
        {
            "type": "devices_any_in_state",
            "state": "open",
            "devices": [
                {"family_id": "tailwind", "device_id": "Left"},
            ],
        },
    )
    assert loaded.type == "devices_any_in_state"
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _tailwind_device_state(
        _FakeTailwindDoor("door-left", "Left", is_open=True),
    )
    result = evaluate_rule(_device_state_rule(loaded), _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "Open: Left" in result.conditions[0].detail


def test_devices_any_in_state_met_when_sonos_playing() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _sonos_device_state(
        _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=True),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Kitchen",
                        family_id=DeviceFamilyId.SONOS,
                    ),
                ],
                state=DeviceConditionState.PLAYING,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "Playing: Kitchen" in result.conditions[0].detail


def test_devices_all_in_state_met_when_sonos_paused() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _sonos_device_state(
        _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=False),
        _FakeSonosZone("RINCON_BBBB", "Living Room", is_playing=False),
    )
    result = evaluate_rule(
        _device_state_rule(
            DevicesAllInStateCondition(
                type="devices_all_in_state",
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Kitchen",
                        family_id=DeviceFamilyId.SONOS,
                    ),
                    RuleConditionDeviceRefOut(
                        device_id="Living Room",
                        family_id=DeviceFamilyId.SONOS,
                    ),
                ],
                state=DeviceConditionState.PAUSED,
            ),
        ),
        _ctx(now=now, device_state=state),
    )
    assert result.all_met is True
    assert "All paused" in result.conditions[0].detail


def test_validate_rule_flags_unsupported_family_for_devices_all_in_state() -> None:
    rule = _device_state_rule(
        DevicesAllInStateCondition(
            type="devices_all_in_state",
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
            state=DeviceConditionState.OPEN,
        ),
    )
    issues = validate_rule(
        rule,
        RuleValidationContext(
            device_state=None,
            geofence_ids=frozenset(),
            roster_name_hint_lookup={},
            roster_user_id_lookup={},
            smtp_configured=False,
        ),
    )
    assert any("cannot report state open" in issue.detail for issue in issues)


def test_validate_rule_flags_unsupported_family_for_devices_any_in_state_on() -> None:
    loaded = _CONDITION_ADAPTER.validate_python(
        {
            "type": "devices_any_in_state",
            "state": "on",
            "devices": [
                {"family_id": "tailwind", "device_id": "Left"},
            ],
        },
    )
    issues = validate_rule(
        _device_state_rule(loaded),
        RuleValidationContext(
            device_state=None,
            geofence_ids=frozenset(),
            roster_name_hint_lookup={},
            roster_user_id_lookup={},
            smtp_configured=False,
        ),
    )
    assert any("cannot report state on" in issue.detail for issue in issues)


def test_validate_rule_flags_unsupported_family_for_devices_any_in_state() -> None:
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
            state=DeviceConditionState.OPEN,
        ),
    )
    issues = validate_rule(
        rule,
        RuleValidationContext(
            device_state=None,
            geofence_ids=frozenset(),
            roster_name_hint_lookup={},
            roster_user_id_lookup={},
            smtp_configured=False,
        ),
    )
    assert any("cannot report state open" in issue.detail for issue in issues)


class _FakeKasaSwitch:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.identifier = host
        self.preferred_label = label
        self._on = is_on

    @property
    def is_on(self) -> bool:
        return self._on


class _FakeSonosZone:
    def __init__(self, identifier: str, label: str, *, is_playing: bool | None) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_playing = is_playing


class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_open = is_open


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


def _device_state_rule(
    condition: (DevicesAllInStateCondition | DevicesAnyInStateCondition | RuleConditionOut),
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[condition]),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="device-state",
        label="Device state",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )


def _kasa_device_state(*switches: _FakeKasaSwitch) -> DeviceManagersState:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(switches)
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _sonos_device_state(*zones: _FakeSonosZone) -> DeviceManagersState:
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = tuple(zones)
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=mgr,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _tailwind_device_state(*doors: _FakeTailwindDoor) -> DeviceManagersState:
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
