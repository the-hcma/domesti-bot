"""Hermetic tests for device-state change detection."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.device_enums import DeviceFamilyId
from app.device_state_change import DeviceStateChangeDetector


def test_device_state_change_detector_ignores_first_sample() -> None:
    on_change = MagicMock()
    detector = DeviceStateChangeDetector(on_change)
    detector.note_bool_state(DeviceFamilyId.TAILWIND, "Left", False)
    on_change.assert_not_called()


def test_device_state_change_detector_notifies_on_transition() -> None:
    on_change = MagicMock()
    detector = DeviceStateChangeDetector(on_change)
    detector.note_bool_state(DeviceFamilyId.TAILWIND, "Left", False)
    detector.note_bool_state(DeviceFamilyId.TAILWIND, "Left", True)
    on_change.assert_called_once_with(DeviceFamilyId.TAILWIND, "Left")
