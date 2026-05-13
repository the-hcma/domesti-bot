"""Live TP-Link Kasa checks via ``python-kasa`` UDP discovery on your LAN.

``KasaDeviceManager.fetch()`` calls :meth:`kasa.Discover.discover`, same discovery path as
``python-kasa discover``. Multi-homed hosts can narrow the broadcast with ``target`` (library
parameter); set ``KASA_DISCOVERY_TARGET`` (for example ``192.168.86.255``) to exercise that path.

Environment::

    export KASA_INTEGRATION=1              # required to run these tests (CI-safe)
    export KASA_DISCOVERY_TARGET=192.168.x.255   # optional subnet broadcast address

If discovery or ``update()`` fails with **authentication** errors on newer hardware, set **both**
``KASA_USERNAME`` and ``KASA_PASSWORD`` (Kasa/Tapo cloud account) and construct the manager with
``credentials=KasaDeviceManager.credentials_from_env()`` — never export only one of them.

Run only these tests::

    pytest tests/test_kasa_integration.py -v -m integration

Use ``-s`` to print discovered switches when tests pass::

    pytest tests/test_kasa_integration.py -v -m integration -s

``test_kasa_fetch_via_default_udp_broadcast`` unsets ``KASA_DISCOVERY_TARGET`` so discovery uses
the library default ``255.255.255.255`` (compare when directed subnet discovery misbehaves).

Without ``KASA_INTEGRATION``, every test **skips**. To print skip reasons::

    pytest tests/test_kasa_integration.py -rs
"""

from __future__ import annotations

import os

import pytest

from app.device_manager import NotInitializedError
from app.kasa_device_manager import KasaDeviceManager
from app.rule_engine import SwitchPowerState


def _kasa_discovery_target_optional() -> str | None:
    t = (os.environ.get("KASA_DISCOVERY_TARGET") or "").strip()
    return t or None


def _mgr_for_integration() -> KasaDeviceManager:
    return KasaDeviceManager(
        discovery_target=_kasa_discovery_target_optional(),
        discovery_timeout=15,
    )


def _print_discovered_switches(mgr: KasaDeviceManager) -> None:
    """Print switch listing (needs ``pytest -s`` to show on success)."""
    print(f"\n--- Kasa switches ---\n{mgr}\n", flush=True)


def _require_kasa_integration() -> None:
    flag = (os.environ.get("KASA_INTEGRATION") or "").strip().lower()
    if flag not in ("1", "yes", "true", "on"):
        pytest.skip(
            "Set KASA_INTEGRATION=1 to run Kasa LAN integration tests (requires hardware on LAN)"
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kasa_disconnect_resets_manager() -> None:
    _require_kasa_integration()
    mgr = _mgr_for_integration()
    await mgr.fetch()
    _print_discovered_switches(mgr)
    assert len(mgr.switches) >= 1
    first_alias = mgr.switches[0].identifier
    await mgr.disconnect()
    assert "not initialized" in str(mgr)
    with pytest.raises(NotInitializedError):
        mgr.get_device_by_alias(first_alias)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kasa_fetch_exposes_switches_and_status_string() -> None:
    _require_kasa_integration()
    async with _mgr_for_integration() as mgr:
        await mgr.fetch()
        _print_discovered_switches(mgr)
        switches = mgr.switches
        assert len(switches) >= 1
        summary = str(mgr)
        assert summary.startswith("KasaDeviceManager:")
        for sw in switches:
            assert sw.identifier in summary
            assert sw.power_state in (SwitchPowerState.ON, SwitchPowerState.OFF)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kasa_fetch_via_default_udp_broadcast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Discover.discover()`` default broadcast when ``KASA_DISCOVERY_TARGET`` is unset."""
    monkeypatch.delenv("KASA_DISCOVERY_TARGET", raising=False)
    _require_kasa_integration()
    async with KasaDeviceManager(discovery_timeout=15) as mgr:
        print(
            "\n--- Kasa: UDP discovery (default broadcast, no KASA_DISCOVERY_TARGET) ---\n",
            flush=True,
        )
        await mgr.fetch()
        _print_discovered_switches(mgr)
        assert len(mgr.switches) >= 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kasa_live_power_matches_cached_switch_state() -> None:
    _require_kasa_integration()
    async with _mgr_for_integration() as mgr:
        await mgr.fetch()
        _print_discovered_switches(mgr)
        for sw in mgr.switches:
            alias = sw.identifier
            live_on = await mgr.is_on(alias)
            live_off = await mgr.is_off(alias)
            assert live_off == (not live_on)
            assert live_on == (sw.power_state == SwitchPowerState.ON)
            assert live_off == (sw.power_state == SwitchPowerState.OFF)
