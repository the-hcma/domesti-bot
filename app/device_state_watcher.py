"""Background watchers that keep each device manager's cached state fresh.

The HTTP UI (``/v1/ui/state``) reads device state from the in-memory
:class:`~app.rule_engine.Device` objects (``is_on`` / ``is_open`` etc.).
Those values only refresh when the manager talks to its hardware: at
initial :meth:`~app.device_manager.DeviceManager.fetch`, or on each
explicit action (``turn_on``, ``open``, ...). Without a watcher the UI
shows stale state whenever the device is operated out-of-band — wall
switch, physical garage button, the vendor's own app, ...

This module fixes that. Every supported backend ships a watcher that
runs forever in the background and reconciles the cached state with the
device's actual state. The default for all current backends is polling
(no LAN device library we use exposes a webhook / event stream today);
when a backend gains a push surface, swap in a subscription-based
implementation of :class:`DeviceStateWatcher` and the lifespan keeps
working unchanged.

Lifecycle (see ``app.api.app``):

1. lifespan begins discovery in the background.
2. when discovery succeeds, lifespan calls :func:`build_default_watchers`
   and hands the resulting list to :func:`run_device_state_watchers`
   in another background task.
3. on shutdown lifespan sets the stop event, awaits the watcher task,
   then tears down the managers.

Configuration: ``DOMESTI_STATE_POLL_INTERVAL_S`` overrides
:data:`DEFAULT_POLL_INTERVAL_S` (must be a positive float).
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Final

from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager, VizioTvDevice

_LOGGER = logging.getLogger(__name__)

_VIZIO_WATCHER_REFRESH_TIMEOUT_S = 5.0

# Default cadence between two polls of the same backend. Generous on
# purpose: LAN devices don't change state often, and the action handlers
# already refresh on every user click, so the watcher's job is to catch
# *external* mutations (wall switch, vendor app). 10s feels live without
# hammering the hardware.
DEFAULT_POLL_INTERVAL_S: float = 10.0

# Minimum interval we'll accept from the env var. Below this we'd be
# DOS-ing the LAN — 1 Hz is already plenty for a hobbyist setup.
_MIN_POLL_INTERVAL_S: float = 1.0

_TRANSIENT_CONNECT_ERRNOS: Final[frozenset[int]] = frozenset(
    {
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTDOWN,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
        errno.ETIMEDOUT,
    }
)


class DeviceStateWatcher(ABC):
    """Run forever (until ``stop`` is set), keeping a manager's cached state fresh."""

    @abstractmethod
    async def run(self, *, stop: asyncio.Event) -> None:
        """Begin polling/subscribing. Returns when ``stop`` is set or cancelled."""


class KasaPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every kasa switch's power state.

    Implementation note: we drive refresh through
    :meth:`KasaDeviceManager.is_on`, which already calls
    ``_kDevice.update()`` and syncs the device's power flag. That's the
    same code path the per-device ``is_on`` HTTP route uses, so there's
    only one place to maintain.
    """

    def __init__(
        self,
        mgr: KasaDeviceManager,
        *,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self) -> None:
        for kd in self._mgr.switches:
            host = (kd._kDevice.host or "").strip() or "?"
            try:
                await self._mgr.is_on(kd.identifier)
            except Exception as exc:
                _log_watcher_refresh_failure(backend="kasa", device_id=host, exc=exc)

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


class SonosPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every Sonos zone's playback state.

    Drives refresh through :meth:`SonosDeviceManager.is_playing`, which
    calls the zone's :meth:`SonosSpeakerDevice.update_playback_state`
    and updates the cached :attr:`SonosSpeakerDevice.is_playing` flag
    in place. The return value is discarded — we only want the side
    effect (the UI reads the cached flag, never blocks on UPnP).

    External mutations the watcher catches: the user pausing playback
    from the Sonos app, AirPlay handing off, or the zone going idle
    after the queue runs out.
    """

    def __init__(
        self,
        mgr: SonosDeviceManager,
        *,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self) -> None:
        for sp in self._mgr.players:
            try:
                await self._mgr.is_playing(sp.identifier)
            except Exception as exc:
                _log_watcher_refresh_failure(
                    backend="sonos",
                    device_id=sp.identifier,
                    exc=exc,
                )

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


class TailwindPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every tailwind door's open/closed state.

    Drives refresh through :meth:`GotailwindDeviceManager.is_open`, which
    calls the controller's per-door ``door_status`` endpoint and syncs
    the cached ``_reported_state`` on the matching
    :class:`GotailwindDevice`. The return value is discarded — we only
    want the side effect.
    """

    def __init__(
        self,
        mgr: GotailwindDeviceManager,
        *,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self) -> None:
        for gd in self._mgr.doors:
            try:
                await self._mgr.is_open(gd.identifier)
            except Exception as exc:
                _log_watcher_refresh_failure(
                    backend="tailwind",
                    device_id=gd.identifier,
                    exc=exc,
                )

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


# TODO(vizio-websocket-watcher): Subscribe via ``PUT /event/register`` and a
# LAN WebSocket for push updates on ``state/device/power_mode``, ``app/current``,
# and ``system/context_change``. Add hermetic tests with a mock WS server plus
# a hardware smoke on a real TV; keep this poller as fallback when registration
# fails (soundbars, older SoCs).
class VizioPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every Vizio TV's cached power state."""

    def __init__(
        self,
        mgr: VizioDeviceManager,
        *,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self) -> None:
        async def _refresh_tv(tv: VizioTvDevice) -> None:
            try:
                await asyncio.wait_for(
                    tv.refresh_power_state(poll=True),
                    timeout=_VIZIO_WATCHER_REFRESH_TIMEOUT_S,
                )
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "[state-watcher vizio] %s update timed out after %.1fs; "
                    "keeping last known state",
                    tv.identifier,
                    _VIZIO_WATCHER_REFRESH_TIMEOUT_S,
                )
            except Exception as exc:
                _log_watcher_refresh_failure(
                    backend="vizio",
                    device_id=tv.identifier,
                    exc=exc,
                )

        await asyncio.gather(*(_refresh_tv(tv) for tv in self._mgr.tvs))

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once()
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._interval_s)
            except asyncio.TimeoutError:
                pass


def _is_transient_connect_failure(exc: BaseException) -> bool:
    os_error = _root_os_error(exc)
    if os_error is None:
        return False
    if os_error.errno in _TRANSIENT_CONNECT_ERRNOS:
        return True
    return isinstance(os_error, ConnectionRefusedError | TimeoutError)


def _log_watcher_refresh_failure(
    *,
    backend: str,
    device_id: str,
    exc: BaseException,
) -> None:
    if _is_transient_connect_failure(exc):
        _LOGGER.warning(
            "[state-watcher %s] %s update failed: %s; keeping last known state",
            backend,
            device_id,
            exc,
        )
        return
    _LOGGER.warning(
        "[state-watcher %s] %s update failed; keeping last known state",
        backend,
        device_id,
        exc_info=True,
    )


def _root_os_error(exc: BaseException) -> OSError | None:
    """Return the first OSError with a non-None errno found anywhere in the chain.

    Walks both ``__cause__`` (explicit chaining via ``raise X from Y``) and
    ``__context__`` (implicit chaining) so that wrappers like
    ``requests.exceptions.ConnectionError`` — which have ``errno=None`` but
    carry a ``ConnectionRefusedError`` deep in the context chain — are
    handled correctly. Falls back to the first OSError found when none has
    errno set.
    """
    seen: set[int] = set()
    work: list[BaseException] = [exc]
    fallback: OSError | None = None
    while work:
        current = work.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, OSError):
            if current.errno is not None:
                return current
            if fallback is None:
                fallback = current
        if current.__cause__ is not None:
            work.append(current.__cause__)
        if current.__context__ is not None:
            work.append(current.__context__)
    return fallback


def build_default_watchers(
    state: DeviceManagersState,
    *,
    interval_s: float,
) -> list[DeviceStateWatcher]:
    """Return the default watcher list for a finished discovery state.

    Skips backends that aren't configured (``--no-tailwind`` etc.).
    AndroidTV is intentionally omitted — bring-up is gated off (see
    ``ANDROIDTV_TEMPORARILY_DISABLED``), so there's nothing to poll.
    """

    watchers: list[DeviceStateWatcher] = [
        KasaPollingWatcher(state.kasa_mgr, interval_s=interval_s),
    ]
    if state.sonos_mgr is not None:
        watchers.append(
            SonosPollingWatcher(state.sonos_mgr, interval_s=interval_s)
        )
    if state.tailwind_mgr is not None:
        watchers.append(
            TailwindPollingWatcher(state.tailwind_mgr, interval_s=interval_s)
        )
    if state.vizio_mgr is not None:
        watchers.append(
            VizioPollingWatcher(state.vizio_mgr, interval_s=interval_s)
        )
    return watchers


def poll_interval_from_env() -> float:
    """Read ``DOMESTI_STATE_POLL_INTERVAL_S`` with validation.

    Returns :data:`DEFAULT_POLL_INTERVAL_S` when unset / empty. Raises
    :class:`ValueError` with a clear message when the value is not a
    positive float or is below :data:`_MIN_POLL_INTERVAL_S` (1.0s).
    """

    raw = (os.environ.get("DOMESTI_STATE_POLL_INTERVAL_S") or "").strip()
    if not raw:
        return DEFAULT_POLL_INTERVAL_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(
            f"Expected a positive float for DOMESTI_STATE_POLL_INTERVAL_S, got {raw!r}"
        ) from exc
    if value < _MIN_POLL_INTERVAL_S:
        raise ValueError(
            f"Expected DOMESTI_STATE_POLL_INTERVAL_S >= {_MIN_POLL_INTERVAL_S}, "
            f"got {value}"
        )
    return value


async def run_device_state_watchers(
    watchers: Iterable[DeviceStateWatcher],
    *,
    stop: asyncio.Event,
) -> None:
    """Run ``watchers`` concurrently until ``stop`` is set.

    Returns immediately when the list is empty (nothing to watch).
    Cancels and awaits every watcher task on shutdown so the caller can
    safely tear down the managers right after this returns. Individual
    watcher exceptions are logged; one bad watcher doesn't take the
    others down.
    """

    materialised = list(watchers)
    if not materialised:
        await stop.wait()
        return
    tasks = [
        asyncio.create_task(
            w.run(stop=stop), name=f"state-watcher-{type(w).__name__}-{i}"
        )
        for i, w in enumerate(materialised)
    ]
    try:
        await stop.wait()
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for t, result in zip(tasks, results, strict=True):
            if isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, BaseException):
                _LOGGER.error(
                    "[state-watcher] %s exited with exception: %r",
                    t.get_name(),
                    result,
                )
