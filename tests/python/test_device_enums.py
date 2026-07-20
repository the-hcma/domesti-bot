"""Round-trip and family support matrix for ``DeviceConditionState``."""

from __future__ import annotations

from app.api.schemas import UIDeviceOut, UIOccupancyReadingsOut
from app.device_enums import (
    EP1_DISPLAY_NAME,
    DeviceConditionState,
    DeviceFamilyId,
    RuleDeviceActionType,
)
from app.rule_engine import expected_state_for_action_type


def test_device_condition_state_wire_values_are_stable() -> None:
    assert {member.value for member in DeviceConditionState} == {
        "clear",
        "closed",
        "occupied",
        "off",
        "on",
        "open",
        "paused",
        "playing",
    }


def test_device_condition_state_desired_bool_matrix() -> None:
    assert DeviceConditionState.OCCUPIED.desired_bool() is True
    assert DeviceConditionState.ON.desired_bool() is True
    assert DeviceConditionState.OPEN.desired_bool() is True
    assert DeviceConditionState.PLAYING.desired_bool() is True
    assert DeviceConditionState.CLEAR.desired_bool() is False
    assert DeviceConditionState.OFF.desired_bool() is False
    assert DeviceConditionState.CLOSED.desired_bool() is False
    assert DeviceConditionState.PAUSED.desired_bool() is False


def test_device_condition_state_supported_by_family_matrix() -> None:
    power = (DeviceConditionState.ON, DeviceConditionState.OFF)
    playback = (DeviceConditionState.PLAYING, DeviceConditionState.PAUSED)
    door = (DeviceConditionState.OPEN, DeviceConditionState.CLOSED)
    occupancy = (DeviceConditionState.OCCUPIED, DeviceConditionState.CLEAR)

    for state in power:
        assert state.supported_by_family(DeviceFamilyId.KASA)
        assert state.supported_by_family(DeviceFamilyId.SONOS)
        assert state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.ANDROIDTV)
        assert not state.supported_by_family(DeviceFamilyId.EP1)

    for state in playback:
        assert state.supported_by_family(DeviceFamilyId.SONOS)
        assert not state.supported_by_family(DeviceFamilyId.KASA)
        assert not state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.EP1)

    for state in door:
        assert state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.KASA)
        assert not state.supported_by_family(DeviceFamilyId.SONOS)
        assert not state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.EP1)

    for state in occupancy:
        assert state.supported_by_family(DeviceFamilyId.EP1)
        assert not state.supported_by_family(DeviceFamilyId.KASA)
        assert not state.supported_by_family(DeviceFamilyId.SONOS)
        assert not state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.ANDROIDTV)


def test_device_family_id_ep1_wire_value_and_display_name() -> None:
    assert DeviceFamilyId.EP1.value == "ep1"
    assert DeviceFamilyId.EP1.display_name() == EP1_DISPLAY_NAME


def test_device_family_id_members_include_ep1() -> None:
    assert {member.value for member in DeviceFamilyId} == {
        "androidtv",
        "ep1",
        "kasa",
        "sonos",
        "tailwind",
        "vizio",
    }


def test_expected_state_for_action_type_maps_to_device_condition_state() -> None:
    assert expected_state_for_action_type(RuleDeviceActionType.TURN_ON) is DeviceConditionState.ON
    assert expected_state_for_action_type(RuleDeviceActionType.TURN_OFF) is DeviceConditionState.OFF
    assert expected_state_for_action_type(RuleDeviceActionType.PAUSE) is DeviceConditionState.PAUSED
    assert expected_state_for_action_type(RuleDeviceActionType.RESUME) is DeviceConditionState.PLAYING
    assert expected_state_for_action_type(RuleDeviceActionType.OPEN) is DeviceConditionState.OPEN
    assert expected_state_for_action_type(RuleDeviceActionType.CLOSE) is DeviceConditionState.CLOSED


def test_ui_device_out_accepts_occupancy_kind_with_readings() -> None:
    device = UIDeviceOut(
        id="aa:bb:cc:dd:ee:ff",
        family_id=DeviceFamilyId.EP1.value,
        label="Office EP1",
        kind="occupancy",
        state=DeviceConditionState.OCCUPIED.value,
        compact_icon="occupancy",
        mac_address="aa:bb:cc:dd:ee:ff",
        occupancy_readings=UIOccupancyReadingsOut(
            humidity_pct=42.5,
            illuminance_lx=120.0,
            temperature_c=21.25,
        ),
    )
    assert device.kind == "occupancy"
    assert device.state == DeviceConditionState.OCCUPIED.value
    assert device.occupancy_readings is not None
    assert device.occupancy_readings.humidity_pct == 42.5
    assert device.occupancy_readings.illuminance_lx == 120.0
    assert device.occupancy_readings.temperature_c == 21.25
    assert device.occupancy_readings.temperature_f == 70.25


def test_ui_device_out_occupancy_readings_default_null_for_other_kinds() -> None:
    device = UIDeviceOut(
        id="aa:bb:cc:dd:ee:ff",
        family_id=DeviceFamilyId.KASA.value,
        label="Desk",
        kind="switch",
        state=DeviceConditionState.ON.value,
        compact_icon="bulb",
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    assert device.occupancy_readings is None


def test_ui_occupancy_readings_out_allows_partial_nulls() -> None:
    readings = UIOccupancyReadingsOut(temperature_c=20.0)
    assert readings.temperature_c == 20.0
    assert readings.temperature_f == 68.0
    assert readings.humidity_pct is None
    assert readings.illuminance_lx is None


def test_ui_occupancy_readings_out_derives_celsius_from_fahrenheit() -> None:
    readings = UIOccupancyReadingsOut(temperature_f=68.0)
    assert readings.temperature_f == 68.0
    assert readings.temperature_c == 20.0


def test_ui_occupancy_readings_out_preserves_both_when_supplied() -> None:
    readings = UIOccupancyReadingsOut(temperature_c=21.25, temperature_f=70.25)
    assert readings.temperature_c == 21.25
    assert readings.temperature_f == 70.25
