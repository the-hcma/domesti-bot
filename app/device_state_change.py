"""Detect cached device-state transitions for rule evaluation."""

from __future__ import annotations

from collections.abc import Callable

from app.device_enums import DeviceFamilyId

DeviceReadingUpdateCallback = Callable[[DeviceFamilyId, str], None]
DeviceStateChangeCallback = Callable[[DeviceFamilyId, str, bool, bool | None], None]


class DeviceStateChangeDetector:
    """Fire callbacks for bool-state transitions and reading/sample wakes."""

    def __init__(
        self,
        on_change: DeviceStateChangeCallback,
        *,
        on_reading_update: DeviceReadingUpdateCallback | None = None,
    ) -> None:
        self._on_change = on_change
        self._on_reading_update = on_reading_update
        self._prior: dict[tuple[DeviceFamilyId, str], bool | None] = {}

    def note_bool_state(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        state: bool | None,
    ) -> None:
        """Record ``state`` and notify when it differs from the prior sample."""
        key = (family_id, device_id)
        prior = self._prior.get(key)
        self._prior[key] = state
        if prior is not None and prior != state:
            self._on_change(family_id, device_id, prior, state)

    def note_reading_update(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        """Notify that a device pushed a fresh reading (every sample, not diffs)."""
        if self._on_reading_update is not None:
            self._on_reading_update(family_id, device_id)
