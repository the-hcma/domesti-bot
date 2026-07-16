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

Watchers run **one asyncio task per backend** (families in parallel).
Within each family, every device in that sweep is refreshed **concurrently**
via :func:`_refresh_all_devices_concurrently` so a slow zone or plug does
not block the rest of the family.

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
import contextlib
import errno
import logging
import os
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable, Coroutine, Iterable
from typing import Any, Final

from app.device_enums import DeviceFamilyId
from app.device_manager import NotInitializedError
from app.device_state_change import DeviceStateChangeDetector
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager

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
        change_detector: DeviceStateChangeDetector | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._change_detector = change_detector
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self, *, stop: asyncio.Event) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        try:
            switches = self._mgr.switches
        except NotInitializedError:
            # Partial bootstrap: Kasa fetch failed while other families are ready.
            # Skip this cycle so the shared watcher task keeps running.
            return
        for kd in switches:
            task_name = f"state-watcher-{DeviceFamilyId.KASA.value}-{kd.identifier}"

            async def _refresh_switch(device: Any = kd) -> None:
                host = (device._kDevice.host or "").strip() or "?"
                try:
                    if not await _await_with_stop(
                        stop,
                        lambda ident=device.identifier: self._mgr.is_on(ident),
                    ):
                        return
                except Exception as exc:
                    _log_watcher_refresh_failure(
                        backend="kasa",
                        device_id=host,
                        exc=exc,
                    )
                    return
                if self._change_detector is not None:
                    self._change_detector.note_bool_state(
                        DeviceFamilyId.KASA,
                        device.identifier,
                        device.is_on,
                    )

            refreshes.append((task_name, _refresh_switch))
        await _refresh_all_devices_concurrently(stop=stop, device_refreshes=refreshes)

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once(stop=stop)
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
        change_detector: DeviceStateChangeDetector | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._change_detector = change_detector
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self, *, stop: asyncio.Event) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        for sp in self._mgr.players:
            task_name = f"state-watcher-{DeviceFamilyId.SONOS.value}-{sp.identifier}"

            async def _refresh_player(device: Any = sp) -> None:
                try:
                    if not await _await_with_stop(
                        stop,
                        lambda ident=device.identifier: self._mgr.is_playing(ident),
                    ):
                        return
                except Exception as exc:
                    _log_watcher_refresh_failure(
                        backend="sonos",
                        device_id=device.identifier,
                        exc=exc,
                    )
                    return
                if self._change_detector is not None:
                    self._change_detector.note_bool_state(
                        DeviceFamilyId.SONOS,
                        device.identifier,
                        device.is_playing,
                    )

            refreshes.append((task_name, _refresh_player))
        await _refresh_all_devices_concurrently(stop=stop, device_refreshes=refreshes)

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once(stop=stop)
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
        change_detector: DeviceStateChangeDetector | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._change_detector = change_detector
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self, *, stop: asyncio.Event) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        for gd in self._mgr.doors:
            task_name = f"state-watcher-{DeviceFamilyId.TAILWIND.value}-{gd.identifier}"

            async def _refresh_door(device: Any = gd) -> None:
                try:
                    if not await _await_with_stop(
                        stop,
                        lambda ident=device.identifier: self._mgr.is_open(ident),
                    ):
                        return
                except Exception as exc:
                    _log_watcher_refresh_failure(
                        backend="tailwind",
                        device_id=device.identifier,
                        exc=exc,
                    )
                    return
                if self._change_detector is not None:
                    self._change_detector.note_bool_state(
                        DeviceFamilyId.TAILWIND,
                        device.identifier,
                        device.is_open,
                    )

            refreshes.append((task_name, _refresh_door))
        await _refresh_all_devices_concurrently(stop=stop, device_refreshes=refreshes)

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once(stop=stop)
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
        change_detector: DeviceStateChangeDetector | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._change_detector = change_detector
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self, *, stop: asyncio.Event) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        for tv in self._mgr.tvs:
            task_name = f"state-watcher-{DeviceFamilyId.VIZIO.value}-{tv.identifier}"

            async def _refresh_tv_device(device: Any = tv) -> None:
                async def _refresh_tv() -> None:
                    await asyncio.wait_for(
                        device.refresh_power_state(poll=True),
                        timeout=_VIZIO_WATCHER_REFRESH_TIMEOUT_S,
                    )

                try:
                    if not await _await_with_stop(stop, _refresh_tv):
                        return
                except asyncio.TimeoutError:
                    _LOGGER.warning(
                        "[state-watcher vizio] %s update timed out after %.1fs; keeping last known state",
                        device.identifier,
                        _VIZIO_WATCHER_REFRESH_TIMEOUT_S,
                    )
                except Exception as exc:
                    _log_watcher_refresh_failure(
                        backend="vizio",
                        device_id=device.identifier,
                        exc=exc,
                    )
                else:
                    if self._change_detector is not None:
                        self._change_detector.note_bool_state(
                            DeviceFamilyId.VIZIO,
                            device.identifier,
                            device.is_on,
                        )

            refreshes.append((task_name, _refresh_tv_device))
        await _refresh_all_devices_concurrently(stop=stop, device_refreshes=refreshes)

    async def run(self, *, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._refresh_once(stop=stop)
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
    change_detector: DeviceStateChangeDetector | None = None,
    interval_s: float,
) -> list[DeviceStateWatcher]:
    """Return the default watcher list for a finished discovery state.

    Skips backends that aren't configured (``--no-tailwind`` etc.).
    AndroidTV is intentionally omitted — bring-up is gated off (see
    ``ANDROIDTV_TEMPORARILY_DISABLED``), so there's nothing to poll.
    """

    watchers: list[DeviceStateWatcher] = [
        KasaPollingWatcher(
            state.kasa_mgr,
            change_detector=change_detector,
            interval_s=interval_s,
        ),
    ]
    if state.sonos_mgr is not None:
        watchers.append(
            SonosPollingWatcher(
                state.sonos_mgr,
                change_detector=change_detector,
                interval_s=interval_s,
            )
        )
    if state.tailwind_mgr is not None:
        watchers.append(
            TailwindPollingWatcher(
                state.tailwind_mgr,
                change_detector=change_detector,
                interval_s=interval_s,
            )
        )
    if state.vizio_mgr is not None:
        watchers.append(
            VizioPollingWatcher(
                state.vizio_mgr,
                change_detector=change_detector,
                interval_s=interval_s,
            )
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
        raise ValueError(f"Expected a positive float for DOMESTI_STATE_POLL_INTERVAL_S, got {raw!r}") from exc
    if value < _MIN_POLL_INTERVAL_S:
        raise ValueError(f"Expected DOMESTI_STATE_POLL_INTERVAL_S >= {_MIN_POLL_INTERVAL_S}, got {value}")
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
        asyncio.create_task(w.run(stop=stop), name=f"state-watcher-{type(w).__name__}-{i}")
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


async def _await_with_stop(
    stop: asyncio.Event,
    awaitable_factory: Callable[[], Coroutine[Any, Any, object]],
) -> bool:
    """Run ``awaitable_factory()`` unless ``stop`` is already set.

    Returns ``True`` when the awaitable finished normally, ``False`` when shutdown
    was requested first (the in-flight task is cancelled).
    """

    if stop.is_set():
        return False
    task = asyncio.create_task(awaitable_factory())
    stop_task = asyncio.create_task(stop.wait())
    async with _cancel_pending_tasks_on_exit(task, stop_task):
        done, _pending = await asyncio.wait(
            {task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            return False
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        await task
        return True


@contextlib.asynccontextmanager
async def _cancel_pending_tasks_on_exit(
    *tasks: asyncio.Task[object],
) -> AsyncIterator[None]:
    try:
        yield
    finally:
        for pending in tasks:
            if not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending


async def _refresh_all_devices_concurrently(
    *,
    stop: asyncio.Event,
    device_refreshes: Iterable[tuple[str, Callable[[], Coroutine[Any, Any, None]]]],
) -> None:
    """Run per-device refresh coroutines in parallel for one poll cycle."""
    if stop.is_set():
        return
    materialised = list(device_refreshes)
    if not materialised:
        return
    device_tasks = [asyncio.create_task(refresh(), name=task_name) for task_name, refresh in materialised]

    async def _gather_device_results() -> list[BaseException | None]:
        return await asyncio.gather(*device_tasks, return_exceptions=True)

    gather_task = asyncio.create_task(
        _gather_device_results(),
        name="state-watcher-gather",
    )
    stop_task = asyncio.create_task(stop.wait(), name="state-watcher-stop-wait")
    async with _cancel_pending_tasks_on_exit(gather_task, stop_task):
        done, _pending = await asyncio.wait(
            {gather_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done:
            for device_task in device_tasks:
                if not device_task.done():
                    device_task.cancel()
            gather_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await gather_task
            return
        stop_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await stop_task
        results = await gather_task
    for (task_name, _refresh), result in zip(materialised, results, strict=True):
        if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
            _LOGGER.warning(
                "[state-watcher] %s failed unexpectedly; keeping last known state",
                task_name,
                exc_info=result,
            )
