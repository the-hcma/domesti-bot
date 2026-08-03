"""EP1 dashboard-header liveness contract (#574).

Header climate/light metrics are green while the sensor is considered
responding and yellow when stale. Liveness is based on subscription
activity (:meth:`Ep1Device.note_heard`), not on climate values changing —
ESPHome may stay quiet for long stretches when the room is still.
"""

from __future__ import annotations

import time

# Align with ``_DEFAULT_EP1_RECONNECT_DELAY_S`` in ``app.device_state_watcher``.
EP1_HEADER_EXPECTED_REFRESH_PERIOD_S: float = 5.0
EP1_HEADER_STALE_AFTER_S: float = 3 * EP1_HEADER_EXPECTED_REFRESH_PERIOD_S


def ep1_is_responding(
    last_heard_at: float | None,
    *,
    now: float | None = None,
) -> bool:
    """True when ``last_heard_at`` is within the header stale window."""

    if last_heard_at is None:
        return False
    clock = time.time() if now is None else now
    return (clock - last_heard_at) <= EP1_HEADER_STALE_AFTER_S
