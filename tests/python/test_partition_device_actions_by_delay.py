"""Hermetic tests for partitioning rule device actions by delay_s."""

from __future__ import annotations

from app.api.schemas import RuleDeviceActionOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.rule_actions import partition_device_actions_by_delay


def test_partition_device_actions_by_delay_splits_immediate_and_delayed() -> None:
    immediate_action = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_OFF,
        device_id="Tuner",
        family_id=DeviceFamilyId.KASA,
    )
    zero_delay = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_OFF,
        delay_s=0,
        device_id="Lamp",
        family_id=DeviceFamilyId.KASA,
    )
    delayed_action = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_ON,
        delay_s=60,
        device_id="Tuner",
        family_id=DeviceFamilyId.KASA,
    )
    immediate, delayed = partition_device_actions_by_delay(
        [immediate_action, zero_delay, delayed_action],
    )
    assert immediate == [immediate_action, zero_delay]
    assert delayed == [delayed_action]


def test_partition_device_actions_by_delay_all_immediate_when_delay_omitted() -> None:
    actions = [
        RuleDeviceActionOut(
            action=RuleDeviceActionType.TURN_ON,
            device_id="A",
            family_id=DeviceFamilyId.KASA,
        ),
        RuleDeviceActionOut(
            action=RuleDeviceActionType.TURN_OFF,
            delay_s=None,
            device_id="B",
            family_id=DeviceFamilyId.KASA,
        ),
    ]
    immediate, delayed = partition_device_actions_by_delay(actions)
    assert immediate == actions
    assert delayed == []


def test_partition_device_actions_by_delay_preserves_order() -> None:
    first = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_OFF,
        delay_s=30,
        device_id="A",
        family_id=DeviceFamilyId.KASA,
    )
    second = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_ON,
        delay_s=90,
        device_id="A",
        family_id=DeviceFamilyId.KASA,
    )
    immediate, delayed = partition_device_actions_by_delay([first, second])
    assert immediate == []
    assert delayed == [first, second]
