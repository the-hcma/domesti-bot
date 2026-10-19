"""Tests for :meth:`KasaDeviceManager.recover_unresponsive` (no hardware).

A plug that lost power stays in the manager as an unresponsive tile (domesti-bot#867).
Recovery reconnects it on its own once it answers again.
"""

from __future__ import annotations

import asyncio
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


@pytest.mark.asyncio
async def test_recover_unresponsive_does_not_overwrite_a_map_installed_while_closing_sessions(tmp_path: Path) -> None:
    """The new map is written before any await, so a rediscover that lands later keeps its result."""

    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    newer_map: dict[str, object] = {}
    real_disconnect = mgr._disconnect_device_map

    async def _disconnect_then_install_newer_map(alias_map: object) -> None:
        mgr._device_name_to_device = newer_map  # type: ignore[assignment]
        await real_disconnect(alias_map)  # type: ignore[arg-type]

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=fan)),
        patch.object(mgr, "_disconnect_device_map", AsyncMock(side_effect=_disconnect_then_install_newer_map)),
    ):
        await mgr.recover_unresponsive()
    assert mgr._device_name_to_device is newer_map


@pytest.mark.asyncio
async def test_recover_unresponsive_stands_down_while_rediscover_is_replacing_the_roster(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    sweep_started = asyncio.Event()
    release_sweep = asyncio.Event()
    connect = AsyncMock(return_value=fan)

    async def _slow_sweep(*_args: object, **_kwargs: object) -> dict[str, object]:
        sweep_started.set()
        await asyncio.wait_for(release_sweep.wait(), timeout=10)
        return {}

    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(side_effect=_slow_sweep)),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", connect),
    ):
        rediscovering = asyncio.create_task(mgr.rediscover())
        await asyncio.wait_for(sweep_started.wait(), timeout=10)
        recovered = await mgr.recover_unresponsive()
        release_sweep.set()
        await rediscovering
    assert recovered == ()
    connect.assert_not_awaited()


@pytest.mark.asyncio
async def test_recover_unresponsive_discards_results_when_a_rediscover_starts_mid_flight(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")

    async def _connect_while_rediscover_starts(*_args: object, **_kwargs: object) -> MagicMock:
        mgr._roster_replacements += 1
        return fan

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=_connect_while_rediscover_starts),
        ),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == ()
    assert [s.unresponsive for s in mgr.switches] == [True]
    fan.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_recover_unresponsive_moves_the_klap_flag_to_the_new_address(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_configs(db, [("192.168.1.50", "Master bedroom fan", _PLUG_CFG, True, _FAN_MAC)])
    mgr = KasaDeviceManager(discovery_cache_path=db, username="owner@example.com", password="pw-for-test")
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.fetch()
    moved = _answering_device(host="192.168.1.77", mac=_FAN_MAC, alias="Master bedroom fan")

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        return moved if cfg.host == "192.168.1.77" else None

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.77"),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
    ):
        assert await mgr.recover_unresponsive() == (_FAN_MAC,)
    assert mgr.hosts_requiring_klap_auth == ("192.168.1.77",)
    assert [(row[0], row[3]) for row in device_discovery_store.load_cached_configs(db)] == [("192.168.1.77", True)]


@pytest.mark.asyncio
async def test_recover_unresponsive_closes_the_session_when_reading_the_plug_fails(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    half_dead = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=half_dead)),
        patch.object(mgr, "_try_ingest_with_credentials", AsyncMock(side_effect=TimeoutError("stuck"))),
    ):
        assert await mgr.recover_unresponsive() == ()
    half_dead.disconnect.assert_awaited()
    assert [s.unresponsive for s in mgr.switches] == [True]


@pytest.mark.asyncio
async def test_recover_unresponsive_stands_down_while_reload_from_cache_is_replacing_the_roster(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    mgr = await _manager_with_unplugged_fan(db)
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    recovered_meanwhile: list[tuple[str, ...]] = []
    real_connect = mgr._connect_devices_from_cache

    async def _connect_and_try_recovery(*args: object, **kwargs: object) -> object:
        # Runs inside reload_from_cache's await: the recovery watcher could fire right now.
        recovered_meanwhile.append(await mgr.recover_unresponsive())
        return await real_connect(*args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=fan)),
        patch.object(mgr, "_connect_devices_from_cache", AsyncMock(side_effect=_connect_and_try_recovery)),
    ):
        await mgr.reload_from_cache()
    assert recovered_meanwhile == [()]


@pytest.mark.asyncio
async def test_overlapping_roster_replacements_keep_recovery_off_until_both_finish(tmp_path: Path) -> None:
    mgr = await _manager_with_unplugged_fan(tmp_path / "cached.sqlite")
    with mgr._replacing_roster():
        with mgr._replacing_roster():
            pass
        # The inner operation finished; the outer one is still replacing the roster.
        assert await mgr.recover_unresponsive() == ()
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=fan)),
    ):
        assert await mgr.recover_unresponsive() == (_FAN_MAC,)


@pytest.mark.asyncio
async def test_recover_unresponsive_never_overwrites_another_stub_at_the_recovered_address(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    other_cfg = {**_PLUG_CFG, "host": "192.168.1.60"}
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.50", "Master bedroom fan", _PLUG_CFG, False, _FAN_MAC),
            ("192.168.1.60", "Visitor lamp", other_cfg, False, "aa:bb:cc:dd:ee:02"),
        ],
    )
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.fetch()
    assert sorted(s.mac_address for s in mgr.switches) == [_FAN_MAC, "aa:bb:cc:dd:ee:02"]
    # The fan comes back, but at the address the other (still silent) stub holds.
    fan = _answering_device(host="192.168.1.60", mac=_FAN_MAC, alias="Master bedroom fan")

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        return fan if cfg.host == "192.168.1.60" else None

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.60"),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
    ):
        recovered = await mgr.recover_unresponsive()
    assert recovered == ()
    assert sorted((s.mac_address, s.host, s.unresponsive) for s in mgr.switches) == [
        (_FAN_MAC, "192.168.1.50", True),
        ("aa:bb:cc:dd:ee:02", "192.168.1.60", True),
    ]
    assert sorted(str(row[4]) for row in device_discovery_store.load_cached_configs(db)) == [
        _FAN_MAC,
        "aa:bb:cc:dd:ee:02",
    ]
    fan.disconnect.assert_awaited()


async def _manager_with_two_unplugged_plugs(db: Path) -> KasaDeviceManager:
    lamp_cfg = {**_PLUG_CFG, "host": "192.168.1.60"}
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.50", "Master bedroom fan", _PLUG_CFG, False, _FAN_MAC),
            ("192.168.1.60", "Visitor lamp", lamp_cfg, False, "aa:bb:cc:dd:ee:02"),
        ],
    )
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.fetch()
    assert len(mgr.switches) == 2
    return mgr


@pytest.mark.asyncio
async def test_cancelled_recovery_closes_sessions_collected_before_a_slow_attempt_finished(tmp_path: Path) -> None:
    """Shutdown or a credentials save cancels a cycle; plugs already reconnected must not leak a session."""

    mgr = await _manager_with_two_unplugged_plugs(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    fan_attempt_returned = asyncio.Event()
    lamp_attempt_blocked = asyncio.Event()
    real_reconnect = mgr._reconnect_unresponsive

    async def _reconnect_and_signal(stub: object, cached: object, qtimeout: int) -> object:
        kd = await real_reconnect(stub, cached, qtimeout)  # type: ignore[arg-type]
        if getattr(stub, "mac_address", None) == _FAN_MAC:
            fan_attempt_returned.set()
        return kd

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        if cfg.host == "192.168.1.50":
            return fan
        await asyncio.wait_for(fan_attempt_returned.wait(), timeout=10)
        lamp_attempt_blocked.set()
        await asyncio.sleep(3600)  # a half-dead plug: only cancellation ends this
        return None

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
        patch.object(mgr, "_reconnect_unresponsive", AsyncMock(side_effect=_reconnect_and_signal)),
    ):
        recovery = asyncio.create_task(mgr.recover_unresponsive())
        await asyncio.wait_for(lamp_attempt_blocked.wait(), timeout=10)
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    fan.disconnect.assert_awaited()
    assert [s.unresponsive for s in mgr.switches] == [True, True]


@pytest.mark.asyncio
async def test_cancelled_recovery_closes_sessions_when_cancelled_during_the_sweep(tmp_path: Path) -> None:
    mgr = await _manager_with_two_unplugged_plugs(tmp_path / "cached.sqlite")
    fan = _answering_device(host="192.168.1.50", mac=_FAN_MAC, alias="Master bedroom fan")
    sweep_started = asyncio.Event()

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        return fan if cfg.host == "192.168.1.50" else None

    async def _slow_sweep(*_args: object, **_kwargs: object) -> dict[str, object]:
        sweep_started.set()
        await asyncio.sleep(3600)
        return {}

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(side_effect=_slow_sweep)),
    ):
        recovery = asyncio.create_task(mgr.recover_unresponsive(sweep=True))
        await asyncio.wait_for(sweep_started.wait(), timeout=10)
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    fan.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_cancelled_sweep_closes_plugs_it_already_matched(tmp_path: Path) -> None:
    mgr = await _manager_with_two_unplugged_plugs(tmp_path / "cached.sqlite")
    moved_fan = _answering_device(host="192.168.1.99", mac=_FAN_MAC, alias="Master bedroom fan")
    stuck = _answering_device(host="192.168.1.98", mac="aa:bb:cc:dd:ee:77", alias="Unrelated plug")
    second_read_started = asyncio.Event()

    async def _ingest(discovered: MagicMock, _qtimeout: int, **_kw: object) -> MagicMock:
        if discovered is stuck:
            second_read_started.set()
            await asyncio.sleep(3600)
        return discovered

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={moved_fan.host: moved_fan, stuck.host: stuck}),
        ),
        patch.object(mgr, "_ingest_discovered_device", AsyncMock(side_effect=_ingest)),
    ):
        recovery = asyncio.create_task(mgr.recover_unresponsive(sweep=True))
        await asyncio.wait_for(second_read_started.wait(), timeout=10)
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
    moved_fan.disconnect.assert_awaited()
    stuck.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_sweep_closes_a_discovered_plug_whose_read_times_out_and_still_finds_the_next(tmp_path: Path) -> None:
    mgr = await _manager_with_two_unplugged_plugs(tmp_path / "cached.sqlite")
    half_dead = _answering_device(host="192.168.1.98", mac="aa:bb:cc:dd:ee:77", alias="Half-dead plug")
    moved_fan = _answering_device(host="192.168.1.99", mac=_FAN_MAC, alias="Master bedroom fan")

    async def _ingest(discovered: MagicMock, _qtimeout: int, **_kw: object) -> MagicMock:
        if discovered is half_dead:
            raise TimeoutError("update timed out")
        return discovered

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={half_dead.host: half_dead, moved_fan.host: moved_fan}),
        ),
        patch.object(mgr, "_ingest_discovered_device", AsyncMock(side_effect=_ingest)),
    ):
        recovered = await mgr.recover_unresponsive(sweep=True)
    assert recovered == (_FAN_MAC,)
    half_dead.disconnect.assert_awaited()
