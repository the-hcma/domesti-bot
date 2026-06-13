"""Tests for :mod:`app.device_state_watcher`.

The watcher's contract is "keep cached device state fresh, forever,
without taking the rest of the app down". These tests pin down:

* :class:`KasaPollingWatcher` / :class:`TailwindPollingWatcher` call
  the corresponding ``is_on`` / ``is_open`` per device per cycle and
  swallow per-device exceptions so one bad device doesn't stop the
  poll;
* the loop exits *promptly* when the stop event is set (no hanging on
  the inter-poll sleep);
* :func:`build_default_watchers` picks the right backends based on
  which managers were configured at boot;
* :func:`poll_interval_from_env` validates user input loudly;
* :func:`run_device_state_watchers` runs concurrent watchers and
  cancels them all on stop, even when one of them raises.

No real hardware is touched — every manager is a :class:`MagicMock`.
"""

from __future__ import annotations

import asyncio
import errno
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.device_state_watcher import (
    DEFAULT_POLL_INTERVAL_S,
    DeviceStateWatcher,
    KasaPollingWatcher,
    SonosPollingWatcher,
    TailwindPollingWatcher,
    build_default_watchers,
    poll_interval_from_env,
    run_device_state_watchers,
)
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager


def _fake_kasa_mgr(identifiers: list[str]) -> KasaDeviceManager:
    """Build a Mock kasa manager whose ``switches`` exposes ``identifier`` only.

    Watcher only needs ``identifier`` and ``_kDevice.host`` (for log
    context) from each switch, so we attach exactly that.
    """

    switches = []
    for ident in identifiers:
        kd = MagicMock()
        kd.identifier = ident
        kd._kDevice.host = ident
        switches.append(kd)
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(switches)
    mgr.is_on = AsyncMock(return_value=True)
    return cast(KasaDeviceManager, mgr)


def _fake_sonos_mgr(identifiers: list[str]) -> SonosDeviceManager:
    """Build a Mock sonos manager whose ``players`` exposes ``identifier``."""

    players = []
    for ident in identifiers:
        sp = MagicMock()
        sp.identifier = ident
        players.append(sp)
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = tuple(players)
    mgr.is_playing = AsyncMock(return_value=False)
    return cast(SonosDeviceManager, mgr)


def _fake_tailwind_mgr(identifiers: list[str]) -> GotailwindDeviceManager:
    """Build a Mock tailwind manager whose ``doors`` exposes ``identifier``."""

    doors = []
    for ident in identifiers:
        gd = MagicMock()
        gd.identifier = ident
        doors.append(gd)
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    mgr.is_open = AsyncMock(return_value=False)
    return cast(GotailwindDeviceManager, mgr)


async def _wait_for_await_count(
    mock: AsyncMock,
    minimum: int,
    *,
    timeout_s: float = 1.0,
) -> None:
    """Poll until ``mock`` has been awaited at least ``minimum`` times."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while mock.await_count < minimum:
        if loop.time() >= deadline:
            break
        await asyncio.sleep(0)
    assert mock.await_count >= minimum, (
        f"Expected at least {minimum} await calls, got {mock.await_count}"
    )


@pytest.mark.asyncio
async def test_kasa_watcher_calls_is_on_per_switch_per_cycle() -> None:
    mgr = _fake_kasa_mgr(["host-a", "host-b"])
    watcher = KasaPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    # Let the loop spin a couple of times: each cycle is 0.01s so 0.05s
    # is plenty for two polls.
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    is_on = cast(AsyncMock, mgr.is_on)
    assert is_on.await_count >= 2
    # Every call must address one of the known identifiers.
    seen_idents = {c.args[0] for c in is_on.await_args_list}
    assert seen_idents == {"host-a", "host-b"}


@pytest.mark.asyncio
async def test_kasa_watcher_keeps_going_when_one_device_raises() -> None:
    mgr = _fake_kasa_mgr(["host-a", "host-b"])
    cast(AsyncMock, mgr.is_on).side_effect = [
        RuntimeError("boom"),  # host-a fails
        True,                  # host-b succeeds
        True,                  # next cycle: host-a
        True,                  # next cycle: host-b
    ]
    watcher = KasaPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    is_on = cast(AsyncMock, mgr.is_on)
    await _wait_for_await_count(is_on, 3)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_kasa_watcher_logs_transient_connect_without_traceback() -> None:
    mgr = _fake_kasa_mgr(["192.168.86.188"])
    connect_error = OSError(113, "Connect call failed ('192.168.86.188', 9999)")
    cast(AsyncMock, mgr.is_on).side_effect = [connect_error, True, True]
    watcher = KasaPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    with patch("app.device_state_watcher._LOGGER.warning") as warning_mock:
        task = asyncio.create_task(watcher.run(stop=stop))
        is_on = cast(AsyncMock, mgr.is_on)
        await _wait_for_await_count(is_on, 2)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert warning_mock.call_count >= 1
    _, kwargs = warning_mock.call_args_list[0]
    assert kwargs.get("exc_info") is not True
    _fmt, backend, device_id, exc = warning_mock.call_args_list[0].args
    assert backend == "kasa"
    assert device_id == "192.168.86.188"
    assert "Connect call failed" in str(exc)


@pytest.mark.asyncio
async def test_kasa_watcher_logs_unexpected_error_with_traceback() -> None:
    mgr = _fake_kasa_mgr(["192.168.86.188"])
    unexpected_error = RuntimeError("unexpected internal failure")
    cast(AsyncMock, mgr.is_on).side_effect = [unexpected_error, True, True]
    watcher = KasaPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    with patch("app.device_state_watcher._LOGGER.warning") as warning_mock:
        task = asyncio.create_task(watcher.run(stop=stop))
        is_on = cast(AsyncMock, mgr.is_on)
        await _wait_for_await_count(is_on, 2)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert warning_mock.call_count >= 1
    _, kwargs = warning_mock.call_args_list[0]
    assert kwargs.get("exc_info") is True


@pytest.mark.asyncio
async def test_kasa_watcher_logs_requests_wrapped_connect_error_without_traceback() -> None:
    """Simulate a requests.ConnectionError that buries ConnectionRefusedError via __context__.

    SoCo (Sonos) and similar requests-based libraries raise their top-level
    exception inside an except block, producing an implicit __context__ chain
    rather than an explicit __cause__ chain. _root_os_error must walk __context__
    to find the errno-carrying OSError several levels deep.
    """
    mgr = _fake_kasa_mgr(["192.168.86.188"])

    # Recreate the chain requests/urllib3 produces:
    #   OuterError (OSError, errno=None)  <-- __context__
    #     NewConnectionError (not OSError) <-- __cause__
    #       ConnectionRefusedError (OSError, errno=111) <- root cause
    root_os_err = ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
    mid_err = Exception("Failed to establish a new connection")
    mid_err.__cause__ = root_os_err
    outer_err = OSError("Max retries exceeded")  # errno=None
    # Implicit __context__ (no 'from'), like requests.adapters.send does
    outer_err.__context__ = mid_err

    cast(AsyncMock, mgr.is_on).side_effect = [outer_err, True, True]
    watcher = KasaPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    with patch("app.device_state_watcher._LOGGER.warning") as warning_mock:
        task = asyncio.create_task(watcher.run(stop=stop))
        is_on = cast(AsyncMock, mgr.is_on)
        await _wait_for_await_count(is_on, 2)
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    assert warning_mock.call_count >= 1
    _, kwargs = warning_mock.call_args_list[0]
    assert kwargs.get("exc_info") is not True


@pytest.mark.asyncio
async def test_kasa_watcher_stops_promptly_when_event_is_set() -> None:
    mgr = _fake_kasa_mgr(["host-a"])
    # 1s between polls. If the loop slept naïvely we'd block here for
    # ~1s before exiting; ``wait_for(stop.wait(), timeout=interval)``
    # exits immediately on ``stop.set()``.
    watcher = KasaPollingWatcher(mgr, interval_s=1.0)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)


@pytest.mark.asyncio
async def test_sonos_watcher_calls_is_playing_per_zone_per_cycle() -> None:
    mgr = _fake_sonos_mgr(["RINCON_A", "RINCON_B"])
    watcher = SonosPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    is_playing = cast(AsyncMock, mgr.is_playing)
    assert is_playing.await_count >= 2
    seen_idents = {c.args[0] for c in is_playing.await_args_list}
    assert seen_idents == {"RINCON_A", "RINCON_B"}


@pytest.mark.asyncio
async def test_sonos_watcher_keeps_going_when_one_zone_raises() -> None:
    mgr = _fake_sonos_mgr(["RINCON_A", "RINCON_B"])
    cast(AsyncMock, mgr.is_playing).side_effect = [
        RuntimeError("upnp boom"),
        False,
        False,
        False,
    ]
    watcher = SonosPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    is_playing = cast(AsyncMock, mgr.is_playing)
    await _wait_for_await_count(is_playing, 3)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_tailwind_watcher_calls_is_open_per_door_per_cycle() -> None:
    mgr = _fake_tailwind_mgr(["door-1", "door-2"])
    watcher = TailwindPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    is_open = cast(AsyncMock, mgr.is_open)
    assert is_open.await_count >= 2
    seen_idents = {c.args[0] for c in is_open.await_args_list}
    assert seen_idents == {"door-1", "door-2"}


@pytest.mark.asyncio
async def test_tailwind_watcher_keeps_going_when_one_door_raises() -> None:
    mgr = _fake_tailwind_mgr(["door-1", "door-2"])
    cast(AsyncMock, mgr.is_open).side_effect = [
        RuntimeError("controller offline"),
        False,
        False,
        False,
    ]
    watcher = TailwindPollingWatcher(mgr, interval_s=0.01)
    stop = asyncio.Event()
    task = asyncio.create_task(watcher.run(stop=stop))
    is_open = cast(AsyncMock, mgr.is_open)
    await _wait_for_await_count(is_open, 3)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


def test_build_default_watchers_omits_optional_when_managers_are_none() -> None:
    kasa = _fake_kasa_mgr(["host-a"])
    state = MagicMock(spec=DeviceManagersState)
    state.kasa_mgr = kasa
    state.sonos_mgr = None
    state.tailwind_mgr = None
    state.vizio_mgr = None

    watchers = build_default_watchers(state, interval_s=5.0)
    assert len(watchers) == 1
    assert isinstance(watchers[0], KasaPollingWatcher)


def test_build_default_watchers_includes_every_configured_backend() -> None:
    kasa = _fake_kasa_mgr(["host-a"])
    sonos = _fake_sonos_mgr(["RINCON_A"])
    tailwind = _fake_tailwind_mgr(["door-1"])
    state = MagicMock(spec=DeviceManagersState)
    state.kasa_mgr = kasa
    state.sonos_mgr = sonos
    state.tailwind_mgr = tailwind
    state.vizio_mgr = None

    watchers = build_default_watchers(state, interval_s=5.0)
    assert len(watchers) == 3
    assert isinstance(watchers[0], KasaPollingWatcher)
    assert isinstance(watchers[1], SonosPollingWatcher)
    assert isinstance(watchers[2], TailwindPollingWatcher)


def test_build_default_watchers_includes_sonos_when_only_sonos_configured() -> None:
    kasa = _fake_kasa_mgr(["host-a"])
    sonos = _fake_sonos_mgr(["RINCON_A"])
    state = MagicMock(spec=DeviceManagersState)
    state.kasa_mgr = kasa
    state.sonos_mgr = sonos
    state.tailwind_mgr = None
    state.vizio_mgr = None

    watchers = build_default_watchers(state, interval_s=5.0)
    assert len(watchers) == 2
    assert isinstance(watchers[1], SonosPollingWatcher)


def test_poll_interval_from_env_defaults_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_STATE_POLL_INTERVAL_S", raising=False)
    assert poll_interval_from_env() == DEFAULT_POLL_INTERVAL_S


def test_poll_interval_from_env_uses_env_value_when_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_STATE_POLL_INTERVAL_S", "2.5")
    assert poll_interval_from_env() == 2.5


def test_poll_interval_from_env_rejects_non_numeric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_STATE_POLL_INTERVAL_S", "fast")
    with pytest.raises(ValueError, match="Expected a positive float"):
        poll_interval_from_env()


def test_poll_interval_from_env_rejects_value_below_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_STATE_POLL_INTERVAL_S", "0.1")
    with pytest.raises(ValueError, match=">= 1.0"):
        poll_interval_from_env()


@pytest.mark.asyncio
async def test_run_device_state_watchers_returns_immediately_for_empty_list() -> None:
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        run_device_state_watchers([], stop=stop),
        timeout=0.5,
    )


@pytest.mark.asyncio
async def test_run_device_state_watchers_runs_concurrent_watchers_until_stop() -> None:
    kasa = _fake_kasa_mgr(["host-a"])
    tailwind = _fake_tailwind_mgr(["door-1"])
    watchers: list[DeviceStateWatcher] = [
        KasaPollingWatcher(kasa, interval_s=0.01),
        TailwindPollingWatcher(tailwind, interval_s=0.01),
    ]
    stop = asyncio.Event()
    task = asyncio.create_task(run_device_state_watchers(watchers, stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)

    # Both watchers must have polled at least once.
    assert cast(AsyncMock, kasa.is_on).await_count >= 1
    assert cast(AsyncMock, tailwind.is_open).await_count >= 1


@pytest.mark.asyncio
async def test_run_device_state_watchers_cancels_all_on_stop() -> None:
    """A long-running watcher must be cancelled when stop fires.

    We use ``interval_s=10`` so the watcher would normally sleep through
    the whole test; the runner must cancel its outstanding ``wait_for``
    rather than waiting for it to time out.
    """

    kasa = _fake_kasa_mgr(["host-a"])
    watcher = KasaPollingWatcher(kasa, interval_s=10.0)
    stop = asyncio.Event()
    task = asyncio.create_task(run_device_state_watchers([watcher], stop=stop))
    await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(task, timeout=0.5)
