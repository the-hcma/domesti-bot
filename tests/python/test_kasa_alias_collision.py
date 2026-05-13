"""Regression tests for :meth:`KasaDeviceManager._fetch_impl` dedup-by-host.

Pre-fix the manager keyed its in-memory device map by ``alias or host``
which silently dropped physically-distinct devices whose Kasa app alias
happened to match (e.g. two outlets the user named ``"Plug"``). The
real-world report: 12 devices in the Kasa app, 9 in domesti-bot.

These tests stub out the python-kasa network layer end-to-end so the
collision logic can be exercised without LAN hardware.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.kasa_device_manager import KasaDeviceManager


def _kdev(host: str, alias: str, *, is_on: bool = False) -> MagicMock:
    """Build a python-kasa-shaped fake device for ``_fetch_impl`` to ingest.

    Only the attributes the manager touches are populated: ``host`` /
    ``alias`` for the lookup, ``is_on`` for ``KasaDevice.__init__``'s
    cached power state, and an awaitable ``update()`` that mirrors the
    real ``KDevice.update`` no-op-on-success contract.
    """

    dev = MagicMock(name=f"KDevice({host})")
    dev.host = host
    dev.alias = alias
    dev.is_on = is_on
    dev.update = AsyncMock()
    dev.disconnect = AsyncMock()
    return dev


@pytest.mark.asyncio
async def test_fetch_keeps_all_devices_even_when_aliases_collide(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two physically distinct outlets named ``"Plug"`` both survive discovery."""

    a = _kdev("192.168.1.10", "Plug", is_on=True)
    b = _kdev("192.168.1.11", "Plug", is_on=False)
    c = _kdev("192.168.1.12", "Other", is_on=True)
    discovered = {a.host: a, b.host: b, c.host: c}

    mgr = KasaDeviceManager()
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value=discovered),
    ):
        with caplog.at_level(logging.WARNING, logger="app.kasa_device_manager"):
            await mgr.fetch()

    # All three devices must be reachable (the bug dropped the second
    # ``"Plug"`` because its identifier collided with the first).
    assert len(mgr.switches) == 3
    hosts = {kd._kDevice.host for kd in mgr.switches}
    assert hosts == {"192.168.1.10", "192.168.1.11", "192.168.1.12"}

    # Each host must be a usable lookup key.
    for host in hosts:
        assert mgr.get_device_by_alias(host) is not None, host

    # The shared alias resolves to *some* device (whichever claimed it
    # first); the duplicate is still reachable by its host.
    assert mgr.get_device_by_alias("Plug") is not None
    assert mgr.get_device_by_alias("Other") is not None

    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "lookup key 'Plug' is shared by" in m for m in warning_messages
    ), warning_messages


@pytest.mark.asyncio
async def test_fetch_does_not_warn_when_all_aliases_unique() -> None:
    """No collision warning fires when every device has a distinct alias."""

    a = _kdev("192.168.1.10", "Living Room Lamp")
    b = _kdev("192.168.1.11", "Kitchen Counter")
    c = _kdev("192.168.1.12", "Office Plug")
    discovered = {a.host: a, b.host: b, c.host: c}

    mgr = KasaDeviceManager()
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value=discovered),
    ):
        with patch.object(KasaDeviceManager, "_expand_kasa_lookup", wraps=mgr._expand_kasa_lookup):
            await mgr.fetch()

    assert len(mgr.switches) == 3
    assert mgr.get_device_by_alias("Living Room Lamp") is not None
    assert mgr.get_device_by_alias("Kitchen Counter") is not None
    assert mgr.get_device_by_alias("Office Plug") is not None


@pytest.mark.asyncio
async def test_fetch_logs_ingest_shortfall_when_some_devices_fail_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``_ingest_discovered_device`` returning ``None`` is surfaced in a WARNING.

    Mirrors the production "discovery found N but only M completed
    recovery" path so a sudden shortfall doesn't go unnoticed.
    """

    a = _kdev("192.168.1.10", "A")
    b = _kdev("192.168.1.11", "B")
    c = _kdev("192.168.1.12", "C")
    discovered = {a.host: a, b.host: b, c.host: c}

    async def _drop_b(self: KasaDeviceManager, dev: MagicMock, qtimeout: int) -> MagicMock | None:
        if dev.host == "192.168.1.11":
            return None
        return dev

    mgr = KasaDeviceManager()
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value=discovered),
    ), patch.object(
        KasaDeviceManager, "_ingest_discovered_device", _drop_b
    ):
        with caplog.at_level(logging.WARNING, logger="app.kasa_device_manager"):
            await mgr.fetch()

    assert len(mgr.switches) == 2
    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    assert any(
        "discovered 3 device(s) on the LAN but only 2 completed" in m
        for m in warning_messages
    ), warning_messages
