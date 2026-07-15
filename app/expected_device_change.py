"""In-memory attribution of device changes caused by UI or the rule engine.

Vacation-mode anomaly alerts (#464) need to know whether a watcher-observed
transition was produced by domesti-bot itself. Call
:func:`mark_expected_device_change` immediately before (or as) an intentional
action is issued; later code calls :func:`consume_expected_device_change` (or
:func:`is_expected_device_change`) within the correlation window.

Keys are ``(family_id, device_id)`` using the same identifiers each control
path already uses (UI host/zone/door id, rule ``device_id`` labels).

Window: default :data:`DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S` (90 s) so
slow garage and Sonos transitions are covered; override with
``DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S`` (positive float seconds, minimum
1.0).
"""

from __future__ import annotations

import logging
import os
import threading
import time

from app.device_enums import DeviceFamilyId

DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S = 90.0


class ExpectedDeviceChangeStore:
    """Thread-safe map of expected ``(family, device_id)`` → expiry monotonic time."""

    def __init__(self) -> None:
        self._expires_at: dict[tuple[DeviceFamilyId, str], float] = {}
        self._lock = threading.Lock()

    def clear(self) -> None:
        """Drop all pending marks (tests)."""
        with self._lock:
            self._expires_at.clear()

    def consume(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Return True and remove the mark when still within the window."""
        key = _key(family_id, device_id)
        if key is None:
            return False
        clock = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(clock)
            expires_at = self._expires_at.pop(key, None)
        return expires_at is not None and clock < expires_at

    def is_expected(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        now: float | None = None,
    ) -> bool:
        """Return True when a non-expired mark exists (does not consume)."""
        key = _key(family_id, device_id)
        if key is None:
            return False
        clock = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(clock)
            expires_at = self._expires_at.get(key)
        return expires_at is not None and clock < expires_at

    def mark(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        now: float | None = None,
        window_s: float | None = None,
    ) -> None:
        """Record that ``device_id`` may change because of a UI / rule action."""
        key = _key(family_id, device_id)
        if key is None:
            _LOGGER.debug(
                "Ignoring expected-change mark with empty device_id family=%s",
                family_id.value,
            )
            return
        duration = (
            resolve_expected_device_change_window_s()
            if window_s is None
            else window_s
        )
        if duration <= 0:
            raise ValueError(f"Expected window_s > 0, got {duration}")
        clock = time.monotonic() if now is None else now
        with self._lock:
            self._prune_locked(clock)
            self._expires_at[key] = clock + duration

    def _prune_locked(self, now: float) -> None:
        stale = [
            key for key, expires_at in self._expires_at.items() if expires_at <= now
        ]
        for key in stale:
            del self._expires_at[key]


expected_device_changes = ExpectedDeviceChangeStore()


def consume_expected_device_change(
    family_id: DeviceFamilyId,
    device_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Return True and clear the mark when the change is still expected."""
    return expected_device_changes.consume(family_id, device_id, now=now)


def is_expected_device_change(
    family_id: DeviceFamilyId,
    device_id: str,
    *,
    now: float | None = None,
) -> bool:
    """Return True when a mark is still live (does not clear it)."""
    return expected_device_changes.is_expected(family_id, device_id, now=now)


def mark_expected_device_change(
    family_id: DeviceFamilyId,
    device_id: str,
    *,
    now: float | None = None,
    window_s: float | None = None,
) -> None:
    """Mark an intentional UI / rule action for the correlation window."""
    expected_device_changes.mark(
        family_id,
        device_id,
        now=now,
        window_s=window_s,
    )


def resolve_expected_device_change_window_s() -> float:
    """Return the correlation window in seconds from env or the default."""
    raw = (os.environ.get(_ENV_WINDOW_S) or "").strip()
    if not raw:
        return DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S
    try:
        value = float(raw)
    except ValueError:
        _LOGGER.warning(
            "Ignoring invalid %s=%r; using default %.1f s",
            _ENV_WINDOW_S,
            raw,
            DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S,
        )
        return DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S
    if value < 1.0:
        _LOGGER.warning(
            "Ignoring %s=%r (must be >= 1.0); using default %.1f s",
            _ENV_WINDOW_S,
            raw,
            DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S,
        )
        return DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S
    return value


_ENV_WINDOW_S = "DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S"
_LOGGER = logging.getLogger(__name__)


def _key(
    family_id: DeviceFamilyId,
    device_id: str,
) -> tuple[DeviceFamilyId, str] | None:
    trimmed = device_id.strip()
    if not trimmed:
        return None
    return (family_id, trimmed)
