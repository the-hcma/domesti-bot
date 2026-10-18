"""Tests for :meth:`KasaDeviceManager.recover_unresponsive` (no hardware).

A plug that lost power stays in the manager as an unresponsive tile (domesti-bot#867).
Recovery reconnects it on its own once it answers again.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kasa.deviceconfig import DeviceConfig

from app import device_discovery_store
from app.kasa_device_manager import KasaDeviceManager

_FAN_MAC = "aa:bb:cc:dd:ee:01"
_PLUG_CFG = {
    "host": "192.168.1.50",
    "timeout": 5,
    "connection_type": {
        "device_family": "IOT.SMARTPLUGSWITCH",
        "encryption_type": "XOR",
        "https": False,
    },
}


def _answering_device(*, host: str, mac: str, alias: str) -> MagicMock:
    dev = MagicMock()
    dev.host = host
    dev.alias = alias
    dev.mac = mac
    dev.sys_info = {}
    dev.is_on = False
    dev.config = DeviceConfig.from_dict({**_PLUG_CFG, "host": host})
    dev.update = AsyncMock()
    dev.disconnect = AsyncMock()
    return dev


async def _manager_with_unplugged_fan(db: Path) -> KasaDeviceManager:
    device_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Master bedroom fan", _PLUG_CFG, False, _FAN_MAC)],
    )
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.fetch()
    assert [switch.unresponsive for switch in mgr.switches] == [True]
    return mgr


@pytest.mark.asyncio
async def test_recover_unresponsive_restores_live_tile_when_plug_answers_at_cached_host(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=fan)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == (_FAN_MAC,)
    assert len(mgr.switches) == 1
    switch = mgr.switches[0]
    assert switch.mac_address == _FAN_MAC
    assert switch.unresponsive is False
    assert switch.kasa_protocol_device() is fan
    assert switch.preferred_label == "Master bedroom fan"


@pytest.mark.asyncio
async def test_recover_unresponsive_follows_arp_to_new_address_and_updates_cache(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    mgr = await _manager_with_unplugged_fan(db)
    fan = _answering_device(host="192.168.1.77", mac=_FAN_MAC, alias="Master bedroom fan")
    connect_hosts: list[str] = []

    async def _connect(
        cfg: DeviceConfig, *, credentials: object, timeout: int, log_failure: bool = True
    ) -> MagicMock | None:
        del credentials, timeout, log_failure
        connect_hosts.append(cfg.host)
        return fan if cfg.host == "192.168.1.77" else None

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.77"),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == (_FAN_MAC,)
    assert connect_hosts == ["192.168.1.50", "192.168.1.77"]
    assert [(row[0], row[4]) for row in device_discovery_store.load_cached_configs(db)] == [("192.168.1.77", _FAN_MAC)]


@pytest.mark.asyncio
async def test_recover_unresponsive_keeps_tile_and_row_when_plug_still_silent(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    mgr = await _manager_with_unplugged_fan(db)
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == ()
    assert [switch.unresponsive for switch in mgr.switches] == [True]
    assert [row[4] for row in device_discovery_store.load_cached_configs(db)] == [_FAN_MAC]


@pytest.mark.asyncio
async def test_recover_unresponsive_ignores_connection_to_a_different_device(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    other = _answering_device(host="192.168.1.50", mac="aa:bb:cc:dd:ee:02", alias="Other plug")
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=other)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == ()
    assert [switch.mac_address for switch in mgr.switches] == [_FAN_MAC]
    assert [switch.unresponsive for switch in mgr.switches] == [True]
    other.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_recover_unresponsive_sweeps_for_plug_at_unknown_address_when_asked(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.99", mac=_FAN_MAC, alias="Master bedroom fan")
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={fan.host: fan})) as discover,
    ):
        without_sweep = await mgr.recover_unresponsive()
        discover.assert_not_awaited()
        with_sweep = await mgr.recover_unresponsive(sweep=True)
    assert without_sweep == ()
    assert with_sweep == (_FAN_MAC,)
    assert mgr.switches[0].unresponsive is False
    assert mgr.switches[0].host == "192.168.1.99"


@pytest.mark.asyncio
async def test_recover_unresponsive_discards_result_when_map_was_replaced_meanwhile(tmp_path: Path) -> None:
    """A rediscover that swaps the device map during recovery wins; the stale reconnect is closed."""

    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    original_map = mgr._device_name_to_device

    async def _connect_then_swap(*_args: object, **_kwargs: object) -> MagicMock:
        mgr._device_name_to_device = dict(original_map or {})
        return fan

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect_then_swap)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == ()
    assert [switch.unresponsive for switch in mgr.switches] == [True]
    fan.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_recover_unresponsive_returns_nothing_before_first_fetch() -> None:
    assert await KasaDeviceManager().recover_unresponsive() == ()
