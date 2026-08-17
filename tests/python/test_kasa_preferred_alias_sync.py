"""Hermetic tests for one-shot preferred_label → Kasa vendor alias sync."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import device_discovery_store
from app.device_label_conflicts import (
    clear_device_label_conflicts,
    drain_device_label_conflicts,
)
from app.kasa_device_manager import KasaDeviceManager


def _kdev(host: str, alias: str, *, mac: str, apply_alias: bool = True) -> MagicMock:
    """python-kasa-shaped fake with an awaitable ``set_alias``.

    Real ``Device.set_alias`` does not assign ``dev.alias``; pass
    ``apply_alias=False`` to match that contract.
    """

    async def _set_alias(new_alias: str) -> dict[str, str]:
        if apply_alias:
            dev.alias = new_alias
        return {}

    dev = MagicMock(name=f"KDevice({host})")
    dev.host = host
    dev.alias = alias
    dev.mac = mac
    dev.sys_info = {}
    dev.is_on = False
    dev.update = AsyncMock()
    dev.disconnect = AsyncMock()
    dev.set_alias = AsyncMock(side_effect=_set_alias)
    dev.config = MagicMock()
    dev.config.to_dict_control_credentials = MagicMock(return_value={"host": host})
    return dev


async def _fetch_with_devices(mgr: KasaDeviceManager, *devices: MagicMock) -> None:
    discovered = {d.host: d for d in devices}
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value=discovered),
    ):
        await mgr.fetch()


@pytest.mark.asyncio
async def test_sync_continues_after_set_alias_failure(tmp_path: Path) -> None:
    fail = _kdev("192.168.1.10", "Outlet A", mac="aa:bb:cc:dd:ee:10")
    fail.set_alias = AsyncMock(side_effect=RuntimeError("device busy"))
    ok = _kdev("192.168.1.11", "Outlet B", mac="aa:bb:cc:dd:ee:11")
    db = tmp_path / "discovery.sqlite"
    mgr = KasaDeviceManager(discovery_cache_path=db)
    await _fetch_with_devices(mgr, fail, ok)
    fail_kd = mgr.get_device_by_alias("aa:bb:cc:dd:ee:10")
    ok_kd = mgr.get_device_by_alias("aa:bb:cc:dd:ee:11")
    assert fail_kd is not None
    assert ok_kd is not None
    fail_kd.set_display_name("Desk")
    ok_kd.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 1
    fail.set_alias.assert_awaited_once_with("Desk")
    ok.set_alias.assert_awaited_once_with("Kitchen lamp")
    cached = device_discovery_store.load_cached_configs(db)
    aliases = {alias for _host, alias, _cfg, _klap, _mac in cached}
    assert "Kitchen lamp" in aliases
    assert "Desk" not in aliases


@pytest.mark.asyncio
async def test_sync_does_not_record_display_name_rename_notices(tmp_path: Path) -> None:
    mac = "aa:bb:cc:dd:ee:10"
    dev = _kdev("192.168.1.10", "Plug", mac=mac)
    db = tmp_path / "discovery.sqlite"
    mgr = KasaDeviceManager(discovery_cache_path=db)
    await _fetch_with_devices(mgr, dev)
    kd = mgr.switches[0]
    kd.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()
    clear_device_label_conflicts()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 1
    assert drain_device_label_conflicts() == ()


@pytest.mark.asyncio
async def test_sync_frees_prior_alias_so_another_device_can_claim_it() -> None:
    a = _kdev("192.168.1.10", "Kitchen lamp", mac="aa:bb:cc:dd:ee:10")
    b = _kdev("192.168.1.11", "Plug", mac="aa:bb:cc:dd:ee:11")
    mgr = KasaDeviceManager()
    await _fetch_with_devices(mgr, b, a)
    desk = mgr.get_device_by_alias("aa:bb:cc:dd:ee:10")
    kitchen = mgr.get_device_by_alias("aa:bb:cc:dd:ee:11")
    assert desk is not None
    assert kitchen is not None
    desk.set_display_name("Desk")
    kitchen.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 2
    a.set_alias.assert_awaited_once_with("Desk")
    b.set_alias.assert_awaited_once_with("Kitchen lamp")


@pytest.mark.asyncio
async def test_sync_persists_preferred_when_set_alias_does_not_mutate_in_memory(
    tmp_path: Path,
) -> None:
    mac = "aa:bb:cc:dd:ee:10"
    dev = _kdev("192.168.1.10", "Plug", mac=mac, apply_alias=False)
    db = tmp_path / "discovery.sqlite"
    mgr = KasaDeviceManager(discovery_cache_path=db)
    await _fetch_with_devices(mgr, dev)
    kd = mgr.switches[0]
    kd.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 1
    dev.set_alias.assert_awaited_once_with("Kitchen lamp")
    assert dev.alias == "Plug"
    cached = device_discovery_store.load_cached_configs(db)
    assert any(alias == "Kitchen lamp" for _host, alias, _cfg, _klap, _mac in cached)


@pytest.mark.asyncio
async def test_sync_preferred_labels_pushes_overlay_onto_vendor_alias(
    tmp_path: Path,
) -> None:
    mac = "aa:bb:cc:dd:ee:10"
    dev = _kdev("192.168.1.10", "Plug", mac=mac)
    db = tmp_path / "discovery.sqlite"
    mgr = KasaDeviceManager(discovery_cache_path=db)
    await _fetch_with_devices(mgr, dev)

    kd = mgr.switches[0]
    kd.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 1
    dev.set_alias.assert_awaited_once_with("Kitchen lamp")
    assert dev.alias == "Kitchen lamp"
    cached = device_discovery_store.load_cached_configs(db)
    assert any(alias == "Kitchen lamp" for _host, alias, _cfg, _klap, _mac in cached)


@pytest.mark.asyncio
async def test_sync_preferred_labels_skips_when_alias_already_matches() -> None:
    mac = "aa:bb:cc:dd:ee:10"
    dev = _kdev("192.168.1.10", "Kitchen lamp", mac=mac)
    mgr = KasaDeviceManager()
    await _fetch_with_devices(mgr, dev)
    kd = mgr.switches[0]
    kd.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 0
    dev.set_alias.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_preferred_labels_skips_when_name_is_mac() -> None:
    mac = "aa:bb:cc:dd:ee:10"
    dev = _kdev("192.168.1.10", mac, mac=mac)
    mgr = KasaDeviceManager()
    await _fetch_with_devices(mgr, dev)

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 0
    dev.set_alias.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_preferred_labels_skips_duplicate_preferred_labels() -> None:
    a = _kdev("192.168.1.10", "Outlet A", mac="aa:bb:cc:dd:ee:10")
    b = _kdev("192.168.1.11", "Outlet B", mac="aa:bb:cc:dd:ee:11")
    mgr = KasaDeviceManager()
    await _fetch_with_devices(mgr, a, b)
    for kd in mgr.switches:
        kd.set_display_name("Plug")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 0
    a.set_alias.assert_not_awaited()
    b.set_alias.assert_not_awaited()
    assert a.alias == "Outlet A"
    assert b.alias == "Outlet B"


@pytest.mark.asyncio
async def test_sync_preferred_labels_does_not_overwrite_other_device_alias() -> None:
    a = _kdev("192.168.1.10", "Desk", mac="aa:bb:cc:dd:ee:10")
    b = _kdev("192.168.1.11", "Kitchen lamp", mac="aa:bb:cc:dd:ee:11")
    mgr = KasaDeviceManager()
    await _fetch_with_devices(mgr, a, b)
    desk = mgr.get_device_by_alias("aa:bb:cc:dd:ee:10")
    assert desk is not None
    desk.set_display_name("Kitchen lamp")
    mgr.rebuild_lookup_after_display_change()

    updated = await mgr.sync_preferred_labels_to_vendor_aliases()
    assert updated == 0
    a.set_alias.assert_not_awaited()
    assert a.alias == "Desk"
