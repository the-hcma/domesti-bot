"""Tests for Sonos / Tailwind / Vizio ``reload_from_cache`` (no LAN discovery)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import device_discovery_store
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager, VizioTvEndpoint


@pytest.mark.asyncio
async def test_sonos_reload_from_cache_never_calls_discover(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen")],
    )
    mgr = SonosDeviceManager(discovery_cache_path=db)
    mgr._alias_to_device = {"RINCON_A": MagicMock()}

    zone = MagicMock()
    zone.uid = "RINCON_A"
    zone.player_name = "Kitchen"
    zone.ip_address = "192.168.1.30"

    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "192.168.1.30", "Kitchen"),
            ("RINCON_B", "192.168.1.31", "Patio"),
        ],
    )
    zone_b = MagicMock()
    zone_b.uid = "RINCON_B"
    zone_b.player_name = "Patio"
    zone_b.ip_address = "192.168.1.31"

    def _soco(host: str):
        if host == "192.168.1.30":
            return zone
        if host == "192.168.1.31":
            return zone_b
        raise AssertionError(f"Unexpected SoCo host {host!r}")

    discover = MagicMock()
    with (
        patch("app.sonos_device_manager.SoCo", side_effect=_soco),
        patch("app.sonos_device_manager.soco_discover", discover),
    ):
        ok = await mgr.reload_from_cache()

    assert ok is True
    assert {p.identifier for p in mgr.players} == {"RINCON_A", "RINCON_B"}
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_tailwind_reload_from_cache_never_calls_mdns(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_tailwind_host(db, "192.168.1.40")
    mgr = GotailwindDeviceManager(
        token="123456",
        host="192.168.1.39",
        display_names_store_path=db,
    )
    mgr._alias_to_device = {"door-1": MagicMock()}
    mgr._host = "192.168.1.39"
    mgr._tailwind = MagicMock()
    mgr._tailwind.close = AsyncMock()

    fake_status = MagicMock()
    fake_status.doors = {}
    new_tw = MagicMock()
    new_tw.__aenter__ = AsyncMock(return_value=new_tw)
    new_tw.status = AsyncMock(return_value=fake_status)
    new_tw.close = AsyncMock()

    discover = AsyncMock()
    with (
        patch(
            "app.gotailwind_device_manager.Tailwind",
            return_value=new_tw,
        ),
        patch(
            "app.gotailwind_device_manager.discover_tailwind_host",
            discover,
        ),
    ):
        ok = await mgr.reload_from_cache(cache_path=db)

    assert ok is True
    assert mgr.host == "192.168.1.40"
    discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_vizio_reload_from_cache_never_calls_ssdp(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.1.50",
        port=7345,
        display_name="Living",
        model="Vizio",
        mac="aa:bb:cc:dd:ee:ff",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="token",
    )
    mgr._initialized = True
    mgr._tvs = ()
    mgr._id_to_tv = {}
    mgr._session = MagicMock()
    mgr._session.closed = False

    fake_tv = MagicMock()
    fake_tv.identifier = "aabbccddeeff"
    fake_tv.preferred_label = "Living"
    fake_tv._client.aclose = AsyncMock()

    ssdp = AsyncMock(return_value=[])
    with (
        patch.object(mgr, "_connect_target", AsyncMock(return_value=(fake_tv, None))),
        patch("app.vizio_device_manager.discover_vizio_hosts_ssdp", ssdp),
    ):
        ok = await mgr.reload_from_cache()

    assert ok is True
    assert len(mgr.tvs) == 1
    ssdp.assert_not_awaited()
