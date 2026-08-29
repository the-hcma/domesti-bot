"""Notify the rule evaluator of bool transitions and reading wakes.

Reading wakes are EP1-only today (``Ep1ReadingMetric`` via ``note_reading``).
For the subscribe contract, wake routing, and how to extend other families,
see ``docs/RULE_ENGINE_PLAN.md`` — *Design: reading-kind subscribe contract
(#670 / #672)*.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.device_enums import DeviceConditionState, DeviceFamilyId, Ep1ReadingMetric

DeviceBoolSeedCallback = Callable[[DeviceFamilyId, str], None]
DeviceBoolTransitionCallback = Callable[[DeviceFamilyId, str, bool, bool | None], None]
DeviceReadingWakeCallback = Callable[[DeviceFamilyId, str, Ep1ReadingMetric], None]

# Public log contracts (asserted by hermetic tests — do not hard-code prose there).
DEVICE_STATE_TRANSITION_LOG = "[device] state-transition family_id=%s device_id=%s metric=%s prior=%s current=%s"
DEVICE_BOOL_METRIC_NAME = "bool"

_LOGGER = logging.getLogger(__name__)


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
            _LOGGER.info(
                DEVICE_STATE_TRANSITION_LOG,
                family_id.value,
                device_id,
                DEVICE_BOOL_METRIC_NAME,
                format_bool_state_label(family_id, prior),
                format_bool_state_label(family_id, state),
            )
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
        reconnect replays are ignored. Numeric reading flips are not logged at
        INFO (illuminance ticks would flood); bool natural-state flips are.
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


def format_bool_state_label(family_id: DeviceFamilyId, value: bool | None) -> str:
    """Return the family's natural-state label for a cached bool (or ``unknown``)."""
    if value is None:
        return "unknown"
    match family_id:
        case DeviceFamilyId.EP1:
            return DeviceConditionState.OCCUPIED.value if value else DeviceConditionState.CLEAR.value
        case DeviceFamilyId.TAILWIND:
            return DeviceConditionState.OPEN.value if value else DeviceConditionState.CLOSED.value
        case DeviceFamilyId.SONOS:
            return DeviceConditionState.PLAYING.value if value else DeviceConditionState.PAUSED.value
        case _:
            return DeviceConditionState.ON.value if value else DeviceConditionState.OFF.value
