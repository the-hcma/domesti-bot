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


def test_device_state_change_detector_note_reading_update_invokes_every_sample() -> None:
    on_change = MagicMock()
    on_reading = MagicMock()
    detector = DeviceStateChangeDetector(on_change, on_reading_update=on_reading)
    detector.note_reading_update(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    detector.note_reading_update(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    assert on_reading.call_count == 2
    on_reading.assert_called_with(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    on_change.assert_not_called()


def test_device_state_change_detector_note_reading_update_noop_without_callback() -> None:
    on_change = MagicMock()
    detector = DeviceStateChangeDetector(on_change)
    detector.note_reading_update(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    on_change.assert_not_called()


def test_device_state_change_detector_notifies_on_transition() -> None:
    on_change = MagicMock()
    detector = DeviceStateChangeDetector(on_change)
    detector.note_bool_state(DeviceFamilyId.TAILWIND, "Left", False)
    detector.note_bool_state(DeviceFamilyId.TAILWIND, "Left", True)
    on_change.assert_called_once_with(
        DeviceFamilyId.TAILWIND,
        "Left",
        False,
        True,
    )
