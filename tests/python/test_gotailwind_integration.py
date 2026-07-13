"""Live Tailwind (GoTailwind) checks against hardware on your LAN.

Requires firmware ≥ 10.10 and the **Local Control Key** (6-digit ``token``): see
``gotailwind_device_manager`` module docstring for dashboard steps.

Environment::

    export TAILWIND_TOKEN=xxxxxxxx
    export TAILWIND_HOST=192.168.1.50   # optional; if omitted, ``fetch()`` uses mDNS discovery

Run only these tests::

    pytest tests/test_gotailwind_integration.py -v -m integration

Use ``-s`` (no stdout capture) to **see discovered doors printed** when tests pass::

    pytest tests/test_gotailwind_integration.py -v -m integration -s

``test_gotailwind_fetch_via_mdns_when_host_unset`` **unsets** ``TAILWIND_HOST`` so ``fetch()``
must resolve the controller via mDNS (same mechanism as ``tailwind scan``). If it fails,
compare with ``tailwind scan`` on the same machine/VLAN and check firewalls or VPNs blocking
UDP 5353.

Without ``TAILWIND_TOKEN``, every test **skips** (CI-safe). To print skip reasons::

    pytest tests/test_gotailwind_integration.py -rs
"""

from __future__ import annotations

import ipaddress
import os

import pytest

from app.device_enums import DeviceConditionState
from app.device_manager import NotInitializedError
from app.gotailwind_device_manager import GotailwindDeviceManager


def _print_discovered_devices(mgr: GotailwindDeviceManager) -> None:
    """Print resolved host and door listing (needs ``pytest -s`` to show on success)."""
    print(f"\n--- Tailwind devices @ {mgr.host} ---\n{mgr}\n", flush=True)


def _tailwind_host_optional() -> str | None:
    host = (os.environ.get("TAILWIND_HOST") or "").strip()
    return host or None


def _tailwind_token_required() -> str:
    token = (os.environ.get("TAILWIND_TOKEN") or "").strip()
    if not token:
        pytest.skip("Set TAILWIND_TOKEN to run Tailwind integration tests")
    return token


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gotailwind_disconnect_resets_manager() -> None:
    token = _tailwind_token_required()
    host = _tailwind_host_optional()
    mgr = GotailwindDeviceManager(host=host, token=token)
    await mgr.fetch()
    _print_discovered_devices(mgr)
    assert len(mgr.doors) >= 1
    assert mgr.host is not None
    await mgr.disconnect()
    assert mgr.host is None
    assert "not initialized" in str(mgr)
    with pytest.raises(NotInitializedError):
        mgr.get_device_by_alias("0")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gotailwind_fetch_exposes_doors_and_status_string() -> None:
    token = _tailwind_token_required()
    host = _tailwind_host_optional()
    async with GotailwindDeviceManager(host=host, token=token) as mgr:
        await mgr.fetch()
        _print_discovered_devices(mgr)
        doors = mgr.doors
        assert len(doors) >= 1
        resolved = mgr.host
        assert resolved is not None
        summary = str(mgr)
        assert f"GotailwindDeviceManager(host={resolved})" in summary
        for door in doors:
            assert f"door {door.door_index}" in summary
            assert door.identifier in summary
            assert door.door_state in (DeviceConditionState.OPEN, DeviceConditionState.CLOSED)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gotailwind_fetch_via_mdns_when_host_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``fetch()`` resolves the controller via mDNS when ``host`` and ``TAILWIND_HOST`` are unset."""
    monkeypatch.delenv("TAILWIND_HOST", raising=False)
    token = _tailwind_token_required()
    async with GotailwindDeviceManager(token=token, discovery_timeout=25.0) as mgr:
        print(
            "\n--- GoTailwind: mDNS inside fetch() (TAILWIND_HOST unset) ---\n",
            flush=True,
        )
        await mgr.fetch()
        _print_discovered_devices(mgr)
        assert mgr.host is not None
        ipaddress.ip_address(mgr.host)
        assert len(mgr.doors) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gotailwind_live_state_matches_cached_door_state() -> None:
    token = _tailwind_token_required()
    host = _tailwind_host_optional()
    async with GotailwindDeviceManager(host=host, token=token) as mgr:
        await mgr.fetch()
        _print_discovered_devices(mgr)
        for door in mgr.doors:
            alias = door.identifier
            live_open = await mgr.is_open(alias)
            live_closed = await mgr.is_closed(alias)
            assert live_open != live_closed
            assert live_open == (door.door_state == DeviceConditionState.OPEN)
            assert live_closed == (door.door_state == DeviceConditionState.CLOSED)
