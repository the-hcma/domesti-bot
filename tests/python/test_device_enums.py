"""Round-trip and family support matrix for ``DeviceConditionState``."""

from __future__ import annotations

from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleDeviceActionType
from app.rule_engine import expected_state_for_action_type


def test_device_condition_state_wire_values_are_stable() -> None:
    assert {member.value for member in DeviceConditionState} == {
        "closed",
        "off",
        "on",
        "open",
        "paused",
        "playing",
    }


def test_device_condition_state_desired_bool_matrix() -> None:
    assert DeviceConditionState.ON.desired_bool() is True
    assert DeviceConditionState.OPEN.desired_bool() is True
    assert DeviceConditionState.PLAYING.desired_bool() is True
    assert DeviceConditionState.OFF.desired_bool() is False
    assert DeviceConditionState.CLOSED.desired_bool() is False
    assert DeviceConditionState.PAUSED.desired_bool() is False


def test_device_condition_state_supported_by_family_matrix() -> None:
    power = (DeviceConditionState.ON, DeviceConditionState.OFF)
    playback = (DeviceConditionState.PLAYING, DeviceConditionState.PAUSED)
    door = (DeviceConditionState.OPEN, DeviceConditionState.CLOSED)

    for state in power:
        assert state.supported_by_family(DeviceFamilyId.KASA)
        assert state.supported_by_family(DeviceFamilyId.SONOS)
        assert state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.ANDROIDTV)

    for state in playback:
        assert state.supported_by_family(DeviceFamilyId.SONOS)
        assert not state.supported_by_family(DeviceFamilyId.KASA)
        assert not state.supported_by_family(DeviceFamilyId.VIZIO)
        assert not state.supported_by_family(DeviceFamilyId.TAILWIND)

    for state in door:
        assert state.supported_by_family(DeviceFamilyId.TAILWIND)
        assert not state.supported_by_family(DeviceFamilyId.KASA)
        assert not state.supported_by_family(DeviceFamilyId.SONOS)
        assert not state.supported_by_family(DeviceFamilyId.VIZIO)


def test_expected_state_for_action_type_maps_to_device_condition_state() -> None:
    assert expected_state_for_action_type(RuleDeviceActionType.TURN_ON) is DeviceConditionState.ON
    assert expected_state_for_action_type(RuleDeviceActionType.TURN_OFF) is DeviceConditionState.OFF
    assert expected_state_for_action_type(RuleDeviceActionType.PAUSE) is DeviceConditionState.PAUSED
    assert expected_state_for_action_type(RuleDeviceActionType.RESUME) is DeviceConditionState.PLAYING
    assert expected_state_for_action_type(RuleDeviceActionType.OPEN) is DeviceConditionState.OPEN
    assert expected_state_for_action_type(RuleDeviceActionType.CLOSE) is DeviceConditionState.CLOSED
