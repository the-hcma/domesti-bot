"""Notify the rule evaluator of bool transitions and reading wakes.

Reading wakes are EP1-only today (``Ep1ReadingMetric`` via ``note_reading``).
For the subscribe contract, wake routing, and how to extend other families,
see ``docs/RULE_ENGINE_PLAN.md`` — *Design: reading-kind subscribe contract
(#670 / #672)*.
"""

from __future__ import annotations

from collections.abc import Callable

from app.device_enums import DeviceFamilyId, Ep1ReadingMetric

DeviceBoolSeedCallback = Callable[[DeviceFamilyId, str], None]
DeviceBoolTransitionCallback = Callable[[DeviceFamilyId, str, bool, bool | None], None]
DeviceReadingWakeCallback = Callable[[DeviceFamilyId, str, Ep1ReadingMetric], None]


class DeviceRuleWakeNotifier:
    """Fire callbacks for bool-state transitions and reading/sample wakes."""

    def __init__(
        self,
        on_bool_transition: DeviceBoolTransitionCallback,
        *,
        on_bool_seed: DeviceBoolSeedCallback | None = None,
        on_reading: DeviceReadingWakeCallback | None = None,
    ) -> None:
        self._on_bool_seed = on_bool_seed
        self._on_bool_transition = on_bool_transition
        self._on_reading = on_reading
        self._prior_bool: dict[tuple[DeviceFamilyId, str], bool | None] = {}
        self._prior_reading: dict[tuple[DeviceFamilyId, str, Ep1ReadingMetric], float] = {}

    def note_bool_transition(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        state: bool | None,
    ) -> bool:
        """Record ``state`` and notify on transition or first known sample.

        Returns ``True`` when a transition or first-sample seed callback fired.
        First-sample seeds call ``on_bool_seed`` (rule eval only) so vacation
        anomaly handling still requires a real prior→current transition.
        """
        key = (family_id, device_id)
        prior = self._prior_bool.get(key)
        self._prior_bool[key] = state
        if prior is not None and prior != state:
            self._on_bool_transition(family_id, device_id, prior, state)
            return True
        if prior is None and state is not None and self._on_bool_seed is not None:
            self._on_bool_seed(family_id, device_id)
            return True
        return False

    def note_reading(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        metric: Ep1ReadingMetric,
        value: float,
    ) -> bool:
        """Notify when ``metric``'s numeric sample differs from the prior value.

        The first sample seeds the prior cache and does not wake (same bootstrap
        semantics as bool transitions without ``on_bool_seed``). Unchanged
        reconnect replays are ignored.
        """
        if self._on_reading is None:
            return False
        key = (family_id, device_id, metric)
        prior = self._prior_reading.get(key)
        self._prior_reading[key] = value
        if prior is None or prior == value:
            return False
        self._on_reading(family_id, device_id, metric)
        return True
