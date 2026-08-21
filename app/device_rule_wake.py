"""Notify the rule evaluator of bool transitions and reading wakes."""

from __future__ import annotations

from collections.abc import Callable

from app.device_enums import DeviceFamilyId, Ep1ReadingMetric

DeviceBoolTransitionCallback = Callable[[DeviceFamilyId, str, bool, bool | None], None]
DeviceReadingWakeCallback = Callable[[DeviceFamilyId, str, Ep1ReadingMetric], None]


class DeviceRuleWakeNotifier:
    """Fire callbacks for bool-state transitions and reading/sample wakes."""

    def __init__(
        self,
        on_bool_transition: DeviceBoolTransitionCallback,
        *,
        on_reading: DeviceReadingWakeCallback | None = None,
    ) -> None:
        self._on_bool_transition = on_bool_transition
        self._on_reading = on_reading
        self._prior: dict[tuple[DeviceFamilyId, str], bool | None] = {}

    def note_bool_transition(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        state: bool | None,
    ) -> bool:
        """Record ``state`` and notify when it differs from the prior sample.

        Returns ``True`` when the transition callback fired (a real transition).
        """
        key = (family_id, device_id)
        prior = self._prior.get(key)
        self._prior[key] = state
        if prior is not None and prior != state:
            self._on_bool_transition(family_id, device_id, prior, state)
            return True
        return False

    def note_reading(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        metric: Ep1ReadingMetric,
    ) -> None:
        """Notify that a device pushed a fresh reading of ``metric`` (every sample)."""
        if self._on_reading is not None:
            self._on_reading(family_id, device_id, metric)
