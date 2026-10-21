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
device's actual state. Most backends poll on a cadence; EP1 uses a
push ``subscribe_states`` session (:class:`Ep1SubscriptionWatcher`) because
aioesphomeapi exposes a native event stream. When another backend gains a
push surface, swap in a subscription-based implementation of
:class:`DeviceStateWatcher` the same way.

Watchers run **one asyncio task per backend** (families in parallel).
Within each **polling** family, every device in that sweep is refreshed
**concurrently** via :func:`_refresh_all_devices_concurrently` so a slow
zone or plug does not block the rest of the family. Subscription
watchers instead run **one reconnect loop per device** in parallel.

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
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterable
from typing import Any, Final

from app.device_enums import DeviceConditionState, DeviceFamilyId, Ep1EntityRole, Ep1ReadingMetric
from app.device_mac import try_normalize_mac
from app.device_manager import NotInitializedError, RecoverableDeviceManager
from app.device_rule_wake import DeviceRuleWakeNotifier
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1Device, Ep1DeviceManager
from app.fast_poll_targets import FAST_POLL_INTERVAL_S, FastPollTargets, FastPollTargetsProvider
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDevice, KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager

_LOGGER = logging.getLogger(__name__)

_DEFAULT_EP1_RECONNECT_DELAY_S = 5.0
_EP1_ROLE_TO_READING_METRIC: Final[dict[Ep1EntityRole, Ep1ReadingMetric]] = {
    Ep1EntityRole.HUMIDITY: Ep1ReadingMetric.HUMIDITY_PCT,
    Ep1EntityRole.ILLUMINANCE: Ep1ReadingMetric.ILLUMINANCE_LX,
    Ep1EntityRole.TEMPERATURE: Ep1ReadingMetric.TEMPERATURE_C,
}
_VIZIO_WATCHER_REFRESH_TIMEOUT_S = 5.0
# Default cadence between two polls of the same backend. Generous on
# purpose: LAN devices don't change state often, and the action handlers
# already refresh on every user click, so the watcher's job is to catch
# *external* mutations (wall switch, vendor app). 10s feels live without
# hammering the hardware.
DEFAULT_POLL_INTERVAL_S: float = 10.0

# How often unresponsive devices are retried (domesti-bot#867, #876). A device that lost power is usually
# back within minutes; a powered-off one costs one short connect attempt per cycle, so five minutes keeps
# the LAN quiet while a replugged device returns promptly.
RECOVERY_INTERVAL_S: float = 300.0

# Every Nth recovery cycle also runs one LAN sweep, to find a device whose address changed while it was off
# and that ARP does not know yet. With the default interval: every 30 min.
RECOVERY_SWEEP_EVERY: int = 6

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


class Ep1SubscriptionWatcher(DeviceStateWatcher):
    """Keep EP1 occupancy + climate/light caches fresh via ``subscribe_states``.

    One reconnect loop per discovered sensor runs in parallel. Session
    failures are logged and retried after ``reconnect_delay_s`` so one
    bad host never stops the others.
    """

    def __init__(
        self,
        mgr: Ep1DeviceManager,
        *,
        wake_notifier: DeviceRuleWakeNotifier | None = None,
        reconnect_delay_s: float = _DEFAULT_EP1_RECONNECT_DELAY_S,
    ) -> None:
        self._wake_notifier = wake_notifier
        self._mgr = mgr
        self._reconnect_delay_s = float(reconnect_delay_s)

    async def run(self, *, stop: asyncio.Event) -> None:
        try:
            devices = list(self._mgr.devices)
        except NotInitializedError:
            await stop.wait()
            return
        if not devices:
            await stop.wait()
            return

        tasks = [
            asyncio.create_task(
                self._watch_device(device, stop=stop),
                name=f"state-watcher-{DeviceFamilyId.EP1.value}-{device.identifier}",
            )
            for device in devices
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _watch_device(self, device: Ep1Device, *, stop: asyncio.Event) -> None:
        # A flagged sensor is still watched, so one session that subscribes clears the flag; the
        # loop ends when the operator forgets the sensor (the manager no longer owns the object).
        while not stop.is_set() and self._mgr.owns(device):
            try:
                await self._mgr.run_subscription_session(
                    device,
                    stop=stop,
                    on_reading_updated=self._on_entity_role if self._wake_notifier else None,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_watcher_refresh_failure(
                    backend=DeviceFamilyId.EP1.value,
                    device_id=device.identifier,
                    exc=exc,
                )
            if stop.is_set():
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._reconnect_delay_s)
            except TimeoutError:
                pass

    def _on_entity_role(self, device: Ep1Device, role: Ep1EntityRole) -> None:
        if self._wake_notifier is None:
            return
        if role == Ep1EntityRole.OCCUPANCY:
            state = device.occupancy_state
            occupied: bool | None
            if state == DeviceConditionState.OCCUPIED.value:
                occupied = True
            elif state == DeviceConditionState.CLEAR.value:
                occupied = False
            else:
                occupied = None
            self._wake_notifier.note_bool_transition(
                DeviceFamilyId.EP1,
                device.identifier,
                occupied,
            )
            return
        metric = _EP1_ROLE_TO_READING_METRIC.get(role)
        if metric is None:
            return
        value = self._reading_value_for_metric(device, metric)
        if value is None:
            return
        self._wake_notifier.note_reading(
            DeviceFamilyId.EP1,
            device.identifier,
            metric,
            value,
        )

    @staticmethod
    def _reading_value_for_metric(device: Ep1Device, metric: Ep1ReadingMetric) -> float | None:
        match metric:
            case Ep1ReadingMetric.HUMIDITY_PCT:
                return device.humidity_pct
            case Ep1ReadingMetric.ILLUMINANCE_LX:
                return device.illuminance_lx
            case Ep1ReadingMetric.TEMPERATURE_C:
                return device.temperature_c
            case _:
                return None


class KasaPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every kasa switch's power state.

    Implementation note: we drive refresh through
    :meth:`KasaDeviceManager.is_on`, which already calls
    ``_kDevice.update()`` and syncs the device's power flag. That's the
    same code path the per-device ``is_on`` HTTP route uses, so there's
    only one place to maintain.

    Every device is read each ``interval_s``. Devices that a turn-on rule is
    watching inside its time window (``fast_poll_targets``) are read every
    ``fast_interval_s`` as well, so that rule reacts sooner. The provider is
    consulted every ``fast_interval_s``; with no such device the devices are
    still read only every ``interval_s``.
    """

    def __init__(
        self,
        mgr: KasaDeviceManager,
        *,
        clock: Callable[[], float] = time.monotonic,
        fast_interval_s: float = FAST_POLL_INTERVAL_S,
        fast_poll_targets: FastPollTargetsProvider | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        sleep: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
        wake_notifier: DeviceRuleWakeNotifier | None = None,
    ) -> None:
        self._clock = clock
        self._fast_interval_s = fast_interval_s
        self._fast_poll_targets = fast_poll_targets
        self._interval_s = interval_s
        self._mgr = mgr
        self._sleep = sleep if sleep is not None else _sleep_unless_stopped
        self._targets_failed = False
        self._wake_notifier = wake_notifier

    async def run(self, *, stop: asyncio.Event) -> None:
        last_full_at: float | None = None
        while not stop.is_set():
            targets = await self._current_targets()
            now = self._clock()
            if last_full_at is None or now - last_full_at >= self._interval_s:
                await self._refresh_once(stop=stop)
                last_full_at = self._clock()
            elif targets:
                await self._refresh_once(stop=stop, only=targets)
            # Wake at the fast interval even with nothing to poll fast, so a window that
            # opens mid-sleep is noticed within one fast tick; full refreshes stay on interval_s.
            until_full_s = max(0.0, last_full_at + self._interval_s - self._clock())
            await self._sleep(stop, min(self._fast_interval_s, until_full_s))

    async def _current_targets(self) -> FastPollTargets:
        """The devices to poll fast now; any failure means "none", never a dead watcher."""
        if self._fast_poll_targets is None:
            return frozenset()
        try:
            targets = await asyncio.to_thread(self._fast_poll_targets)
        except Exception:
            if not self._targets_failed:
                _LOGGER.exception("[state-watcher] fast-poll targets unavailable; using the global interval")
            self._targets_failed = True
            return frozenset()
        self._targets_failed = False
        return targets

    async def _refresh_once(self, *, stop: asyncio.Event, only: FastPollTargets | None = None) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        try:
            switches = self._mgr.switches
        except NotInitializedError:
            # Partial bootstrap: Kasa fetch failed while other families are ready.
            # Skip this cycle so the shared watcher task keeps running.
            return
        for kd in switches:
            if only is not None and not _is_fast_poll_target(kd, only):
                continue
            task_name = f"state-watcher-{DeviceFamilyId.KASA.value}-{kd.identifier}"

            async def _refresh_switch(device: Any = kd) -> None:
                if device._kDevice is None:
                    return
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
                if self._wake_notifier is not None:
                    self._wake_notifier.note_bool_transition(
                        DeviceFamilyId.KASA,
                        device.identifier,
                        device.is_on,
                    )

            refreshes.append((task_name, _refresh_switch))
        await _refresh_all_devices_concurrently(stop=stop, device_refreshes=refreshes)


class SonosPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every Sonos speaker's playback state.

    Drives refresh through :meth:`SonosDeviceManager.is_playing`, which
    calls the speaker's :meth:`SonosSpeakerDevice.update_playback_state`
    and updates the cached :attr:`SonosSpeakerDevice.is_playing` flag
    in place. The return value is discarded — we only want the side
    effect (the UI reads the cached flag, never blocks on UPnP).

    External mutations the watcher catches: the user pausing playback
    from the Sonos app, AirPlay handing off, or the speaker going idle
    after the queue runs out.
    """

    def __init__(
        self,
        mgr: SonosDeviceManager,
        *,
        wake_notifier: DeviceRuleWakeNotifier | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._wake_notifier = wake_notifier
        self._mgr = mgr
        self._interval_s = interval_s

    async def _refresh_once(self, *, stop: asyncio.Event) -> None:
        refreshes: list[tuple[str, Callable[[], Coroutine[Any, Any, None]]]] = []
        for sp in self._mgr.players:
            task_name = f"state-watcher-{DeviceFamilyId.SONOS.value}-{sp.identifier}"

            async def _refresh_player(device: Any = sp) -> None:
                # A flagged speaker is polled too: one answered read is what clears the flag.
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
                if self._wake_notifier is not None:
                    self._wake_notifier.note_bool_transition(
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
        wake_notifier: DeviceRuleWakeNotifier | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._wake_notifier = wake_notifier
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
                if self._wake_notifier is not None:
                    self._wake_notifier.note_bool_transition(
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
class UnresponsiveRecoveryWatcher(DeviceStateWatcher):
    """Bring a family's unresponsive devices back by themselves once they answer again.

    Every ``interval_s`` it asks the manager's ``recover_unresponsive`` to retry the devices that stopped
    answering (a device that lost power is kept as an unresponsive tile, domesti-bot#867); every
    ``sweep_every``-th cycle the retry also runs one LAN sweep. With no such device a cycle does nothing. It
    waits one interval before the first cycle, since discovery just ran, and never lets one failed cycle
    stop the loop. One watcher runs per family; ``family`` only names it in the log.
    """

    def __init__(
        self,
        mgr: RecoverableDeviceManager,
        *,
        family: DeviceFamilyId,
        interval_s: float = RECOVERY_INTERVAL_S,
        sleep: Callable[[asyncio.Event, float], Awaitable[None]] | None = None,
        sweep_every: int = RECOVERY_SWEEP_EVERY,
    ) -> None:
        if sweep_every < 1:
            raise ValueError(f"Expected sweep_every >= 1, got {sweep_every}")
        self._family = family
        self._interval_s = interval_s
        self._mgr = mgr
        self._sleep = sleep if sleep is not None else _sleep_unless_stopped
        self._sweep_every = sweep_every

    @property
    def family(self) -> DeviceFamilyId:
        return self._family

    @property
    def interval_s(self) -> float:
        return self._interval_s

    async def run(self, *, stop: asyncio.Event) -> None:
        cycle = 0
        while not stop.is_set():
            await self._sleep(stop, self._interval_s)
            if stop.is_set():
                return
            cycle += 1
            try:
                await _await_with_stop(
                    stop,
                    lambda sweep=(cycle % self._sweep_every == 0): self._mgr.recover_unresponsive(sweep=sweep),
                )
            except Exception:
                _LOGGER.exception("[state-watcher %s] recovery cycle failed; will retry", self._family.value)

    @property
    def sweep_every(self) -> int:
        return self._sweep_every


class VizioPollingWatcher(DeviceStateWatcher):
    """Periodically re-read every Vizio TV's cached power state."""

    def __init__(
        self,
        mgr: VizioDeviceManager,
        *,
        wake_notifier: DeviceRuleWakeNotifier | None = None,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> None:
        self._wake_notifier = wake_notifier
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
                    if self._wake_notifier is not None:
                        self._wake_notifier.note_bool_transition(
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


def _is_fast_poll_target(device: KasaDevice, targets: FastPollTargets) -> bool:
    """Whether a Kasa device is one of the ``(family, device)`` pairs to poll fast right now."""
    keys = {device.identifier}
    mac = try_normalize_mac(device.mac_address or "")
    if mac is not None:
        keys.add(mac)
    return any((DeviceFamilyId.KASA, key) in targets for key in keys)


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


async def _sleep_unless_stopped(stop: asyncio.Event, delay_s: float) -> None:
    """Wait ``delay_s`` seconds, or until ``stop`` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=delay_s)


def build_default_watchers(
    state: DeviceManagersState,
    *,
    fast_poll_targets: FastPollTargetsProvider | None = None,
    wake_notifier: DeviceRuleWakeNotifier | None = None,
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
            fast_poll_targets=fast_poll_targets,
            wake_notifier=wake_notifier,
            interval_s=interval_s,
        ),
        UnresponsiveRecoveryWatcher(state.kasa_mgr, family=DeviceFamilyId.KASA),
    ]
    if state.ep1_mgr is not None:
        watchers.append(
            Ep1SubscriptionWatcher(
                state.ep1_mgr,
                wake_notifier=wake_notifier,
            )
        )
        watchers.append(UnresponsiveRecoveryWatcher(state.ep1_mgr, family=DeviceFamilyId.EP1))
    if state.sonos_mgr is not None:
        watchers.append(
            SonosPollingWatcher(
                state.sonos_mgr,
                wake_notifier=wake_notifier,
                interval_s=interval_s,
            )
        )
        watchers.append(UnresponsiveRecoveryWatcher(state.sonos_mgr, family=DeviceFamilyId.SONOS))
    if state.tailwind_mgr is not None:
        watchers.append(
            TailwindPollingWatcher(
                state.tailwind_mgr,
                wake_notifier=wake_notifier,
                interval_s=interval_s,
            )
        )
    if state.vizio_mgr is not None:
        watchers.append(
            VizioPollingWatcher(
                state.vizio_mgr,
                wake_notifier=wake_notifier,
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
