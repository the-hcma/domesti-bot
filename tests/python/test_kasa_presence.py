"""Presence notices raised by :class:`KasaDeviceManager` (domesti-bot#867); no hardware."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kasa.deviceconfig import DeviceConfig

from app import device_discovery_store
from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence import DevicePresenceStore
from app.kasa_device_manager import UNRESPONSIVE_AFTER_FAILED_POLLS, KasaDeviceManager

_FAN_MAC = "aa:bb:cc:dd:ee:01"
_LAMP_MAC = "aa:bb:cc:dd:ee:02"
_CFG = {
    "host": "192.168.1.50",
    "timeout": 5,
    "connection_type": {
        "device_family": "IOT.SMARTPLUGSWITCH",
        "encryption_type": "XOR",
        "https": False,
    },
}


def _device(*, host: str = "192.168.1.50", mac: str = _FAN_MAC, alias: str = "Master bedroom fan") -> MagicMock:
    dev = MagicMock()
    dev.host = host
    dev.alias = alias
    dev.mac = mac
    dev.sys_info = {}
    dev.is_on = False
    dev.config = DeviceConfig.from_dict({**_CFG, "host": host})
    dev.config.to_dict_control_credentials = MagicMock(return_value={**_CFG, "host": host})
    dev.update = AsyncMock()
    dev.disconnect = AsyncMock()
    return dev


async def _live_manager(db: Path, store: DevicePresenceStore, dev: MagicMock) -> KasaDeviceManager:
    device_discovery_store.save_configs(db, [(dev.host, dev.alias, _CFG, False, dev.mac)])
    mgr = KasaDeviceManager(discovery_cache_path=db, presence_store=store)
    with patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=dev)):
        await mgr.fetch()
    assert [s.unresponsive for s in mgr.switches] == [False]
    return mgr


async def _unplugged_manager(db: Path, store: DevicePresenceStore) -> KasaDeviceManager:
    device_discovery_store.save_configs(db, [("192.168.1.50", "Master bedroom fan", _CFG, False, _FAN_MAC)])
    mgr = KasaDeviceManager(discovery_cache_path=db, presence_store=store)
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.fetch()
    return mgr


@pytest.mark.asyncio
async def test_unplugged_cached_plug_is_announced_as_disappeared_at_startup(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    await _unplugged_manager(tmp_path / "c.sqlite", store)
    (notice,) = store.notices()
    assert notice.kind == DevicePresenceKind.DISAPPEARED
    assert (notice.family_id, notice.device_id, notice.label, notice.host) == (
        "kasa",
        _FAN_MAC,
        "Master bedroom fan",
        "192.168.1.50",
    )


@pytest.mark.asyncio
async def test_rediscover_does_not_announce_a_still_missing_plug_twice(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    first = store.notices()
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.rediscover()
    assert store.notices() == first


@pytest.mark.asyncio
async def test_rediscover_that_finds_the_plug_announces_it_as_reappeared(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    fan = _device()
    with patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={fan.host: fan})):
        await mgr.rediscover()
    (notice,) = store.notices()
    assert notice.kind == DevicePresenceKind.REAPPEARED
    assert notice.device_id == _FAN_MAC


@pytest.mark.asyncio
async def test_live_plug_is_flagged_after_consecutive_failed_polls_and_announced(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    dev = _device()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, dev)
    dev.update.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    assert store.notices() == ()
    assert mgr.switches[0].unresponsive is False
    with pytest.raises(TimeoutError):
        await mgr.is_on(_FAN_MAC)
    assert mgr.switches[0].unresponsive is True
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id, notice.label) == (
        DevicePresenceKind.DISAPPEARED,
        _FAN_MAC,
        "Master bedroom fan",
    )


@pytest.mark.asyncio
async def test_a_successful_poll_resets_the_failure_count(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    dev = _device()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, dev)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        dev.update.side_effect = TimeoutError("no answer")
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    dev.update.side_effect = None
    await mgr.is_on(_FAN_MAC)
    dev.update.side_effect = TimeoutError("no answer")
    with pytest.raises(TimeoutError):
        await mgr.is_on(_FAN_MAC)
    assert mgr.switches[0].unresponsive is False
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_flagged_plug_that_answers_a_poll_again_is_announced_as_reappeared(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    dev = _device()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, dev)
    dev.update.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    dev.update.side_effect = None
    await mgr.is_on(_FAN_MAC)
    assert mgr.switches[0].unresponsive is False
    (notice,) = store.notices()
    assert notice.kind == DevicePresenceKind.REAPPEARED


@pytest.mark.asyncio
async def test_recover_unresponsive_announces_the_return(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    fan = _device()
    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=fan)),
    ):
        assert await mgr.recover_unresponsive() == (_FAN_MAC,)
    (notice,) = store.notices()
    assert notice.kind == DevicePresenceKind.REAPPEARED


@pytest.mark.asyncio
async def test_recover_unresponsive_reconnects_a_flagged_live_plug_at_its_new_address(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    old = _device()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, old)
    old.update.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    moved = _device(host="192.168.1.77")

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        return moved if cfg.host == "192.168.1.77" else None

    with (
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.77"),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
    ):
        assert await mgr.recover_unresponsive() == (_FAN_MAC,)
    assert mgr.switches[0].host == "192.168.1.77"
    assert mgr.switches[0].unresponsive is False
    old.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_forget_device_removes_tile_cache_row_and_tracking(tmp_path: Path) -> None:
    db = tmp_path / "c.sqlite"
    store = DevicePresenceStore()
    lamp = _device(host="192.168.1.60", mac=_LAMP_MAC, alias="Lamp")
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.50", "Master bedroom fan", _CFG, False, _FAN_MAC),
            ("192.168.1.60", "Lamp", {**_CFG, "host": "192.168.1.60"}, False, _LAMP_MAC),
        ],
    )
    mgr = KasaDeviceManager(discovery_cache_path=db, presence_store=store)

    async def _connect(cfg: DeviceConfig, **_kw: object) -> MagicMock | None:
        return lamp if cfg.host == "192.168.1.60" else None

    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={lamp.host: lamp})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(side_effect=_connect)),
    ):
        await mgr.fetch()
    assert {s.mac_address for s in mgr.switches} == {_FAN_MAC, _LAMP_MAC}
    assert [n.device_id for n in store.notices()] == [_FAN_MAC]

    assert await mgr.forget_device(_FAN_MAC) is True

    assert [s.mac_address for s in mgr.switches] == [_LAMP_MAC]
    assert [row[4] for row in device_discovery_store.load_cached_configs(db)] == [_LAMP_MAC]
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_forget_device_returns_false_for_an_unknown_mac(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    assert await mgr.forget_device("aa:bb:cc:dd:ee:99") is False
    assert [s.mac_address for s in mgr.switches] == [_FAN_MAC]


@pytest.mark.asyncio
async def test_forgotten_plug_that_goes_missing_again_later_is_not_suppressed(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    notice = store.notices()[0]
    store.keep_checking(notice.notice_id)
    assert store.notices() == ()
    assert await mgr.forget_device(_FAN_MAC) is True
    assert store.get(notice.notice_id) is None
    assert store.note_disappeared(device_id=_FAN_MAC, family_id="kasa", host=None, label="Fan") is not None
    assert DevicePresenceDecision.PENDING == store.notices()[0].decision


@pytest.mark.asyncio
async def test_plug_revived_by_a_control_action_still_announces_its_return(tmp_path: Path) -> None:
    """turn_on clears the flag itself; the next good poll must still close the notice."""

    store = DevicePresenceStore()
    dev = _device()
    dev.turn_on = AsyncMock()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, dev)
    dev.update.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    assert [n.kind for n in store.notices()] == [DevicePresenceKind.DISAPPEARED]
    dev.update.side_effect = None
    await mgr.switches[0].turn_on()
    assert mgr.switches[0].unresponsive is False
    await mgr.is_on(_FAN_MAC)
    assert [n.kind for n in store.notices()] == [DevicePresenceKind.REAPPEARED]


@pytest.mark.asyncio
async def test_rediscover_resets_the_failed_poll_count_of_a_healthy_plug(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    dev = _device()
    mgr = await _live_manager(tmp_path / "c.sqlite", store, dev)
    dev.update.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        with pytest.raises(TimeoutError):
            await mgr.is_on(_FAN_MAC)
    dev.update.side_effect = None
    with patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={dev.host: dev})):
        await mgr.rediscover()
    dev.update.side_effect = TimeoutError("no answer")
    with pytest.raises(TimeoutError):
        await mgr.is_on(_FAN_MAC)
    assert mgr.switches[0].unresponsive is False
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_notice_is_cleared_when_the_missing_plug_leaves_the_roster_another_way(tmp_path: Path) -> None:
    """Dropped by an address collision (not by Forget): its banner must not linger."""

    store = DevicePresenceStore()
    mgr = await _unplugged_manager(tmp_path / "c.sqlite", store)
    assert [n.device_id for n in store.notices()] == [_FAN_MAC]
    occupant = _device(mac=_LAMP_MAC, alias="Lamp")
    with (
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={occupant.host: occupant})),
        patch("app.kasa_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.kasa_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.kasa_device_manager._connect_from_saved_config", AsyncMock(return_value=None)),
    ):
        await mgr.rediscover()
    assert [s.mac_address for s in mgr.switches] == [_LAMP_MAC]
    assert store.notices() == ()
