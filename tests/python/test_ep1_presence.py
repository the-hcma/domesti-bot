# pyright: reportCallIssue=false
"""Presence notices, recovery and forget for EP1 sensors (domesti-bot#876)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioesphomeapi.model import BinarySensorInfo, BinarySensorState

from app.device_discovery_store import load_ep1_devices, upsert_ep1_device
from app.device_manager import UNRESPONSIVE_AFTER_FAILED_POLLS
from app.device_presence import DevicePresenceKind, DevicePresenceStore
from app.ep1_device_manager import Ep1Device, Ep1DeviceManager

_MAC = "aa:bb:cc:dd:ee:01"
_OTHER_MAC = "aa:bb:cc:dd:ee:02"
_HOST = "192.0.2.10"
_MOVED_HOST = "192.0.2.77"


def _client(mac: str, *, name: str = "Office", occupied: bool = True, fail: bool = False) -> MagicMock:
    info = MagicMock()
    info.mac_address = mac.upper()
    info.friendly_name = name
    info.name = name.lower()
    client = MagicMock()
    client.connect = AsyncMock(side_effect=RuntimeError("no route to host") if fail else None)
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=occupied)))
    return client


class _Lan:
    """Maps host to the client that answers there; a host with no client refuses the connection."""

    def __init__(self) -> None:
        self.clients: dict[str, MagicMock] = {}
        self.hosts_tried: list[str] = []

    def factory(self, host: str, port: int, **_kwargs: Any) -> MagicMock:
        del port
        self.hosts_tried.append(host)
        return self.clients.get(host) or _client(_OTHER_MAC, fail=True)


@pytest.fixture(autouse=True)
def _no_real_arp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test off the host ARP table; a test that needs an ARP answer overrides these."""

    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda _mac: None)
    monkeypatch.setattr("app.ep1_device_manager.mac_alive_on_lan", lambda **_k: False)


async def _running_manager(tmp_path: Path, store: DevicePresenceStore, lan: _Lan) -> tuple[Ep1DeviceManager, Ep1Device]:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    lan.clients[_HOST] = _client(_MAC)
    mgr = Ep1DeviceManager(
        discovery_cache_path=cache,
        noise_psk=None,
        presence_store=store,
        zeroconf_discovery=False,
        api_client_factory=lan.factory,
    )
    await mgr.fetch()
    (device,) = mgr.devices
    return mgr, device


async def _session(mgr: Ep1DeviceManager, device: Ep1Device) -> None:
    """One subscription session that ends at once (the stop event is already set)."""

    stop = asyncio.Event()
    stop.set()
    await mgr.run_subscription_session(device, stop=stop)


async def _failed_session(mgr: Ep1DeviceManager, device: Ep1Device, lan: _Lan) -> None:
    lan.clients[device.host] = _client(_MAC, fail=True)
    with pytest.raises(RuntimeError):
        await _session(mgr, device)


@pytest.mark.asyncio
async def test_sensor_is_flagged_after_consecutive_failed_sessions_and_announced(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        await _failed_session(mgr, device, lan)
    assert device.unresponsive is False
    assert store.notices() == ()
    await _failed_session(mgr, device, lan)
    assert device.unresponsive is True
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id, notice.label, notice.host) == (
        DevicePresenceKind.DISAPPEARED,
        _MAC,
        "Office",
        _HOST,
    )


@pytest.mark.asyncio
async def test_a_session_that_subscribes_resets_the_failure_count(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        await _failed_session(mgr, device, lan)
    lan.clients[_HOST] = _client(_MAC)
    await _session(mgr, device)
    await _failed_session(mgr, device, lan)
    assert device.unresponsive is False
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_flagged_sensor_that_subscribes_again_is_announced_as_reappeared(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        await _failed_session(mgr, device, lan)
    lan.clients[_HOST] = _client(_MAC)
    await _session(mgr, device)
    assert device.unresponsive is False
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.REAPPEARED, _MAC)


@pytest.mark.asyncio
async def test_a_rediscover_that_reconnects_a_sensor_starts_a_fresh_failure_streak(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        await _failed_session(mgr, device, lan)
    lan.clients[_HOST] = _client(_MAC)
    await mgr.rediscover()
    (fresh,) = mgr.devices
    await _failed_session(mgr, fresh, lan)
    assert fresh.unresponsive is False
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_a_rediscover_keeps_the_streak_of_a_sensor_it_did_not_reconnect(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        await _failed_session(mgr, device, lan)
    streak = dict(mgr._failed_sessions)
    del lan.clients[_HOST]
    await mgr.rediscover()
    assert mgr._failed_sessions == streak


@pytest.mark.asyncio
async def test_a_stopped_session_is_not_a_failure(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS + 1):
        await _session(mgr, device)
    assert device.unresponsive is False
    assert store.notices() == ()


async def _flagged(tmp_path: Path, store: DevicePresenceStore, lan: _Lan) -> tuple[Ep1DeviceManager, Ep1Device]:
    mgr, device = await _running_manager(tmp_path, store, lan)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        await _failed_session(mgr, device, lan)
    assert device.unresponsive is True
    return mgr, device


@pytest.mark.asyncio
async def test_recovery_reconnects_a_sensor_that_answers_at_its_last_address(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    lan.clients[_HOST] = _client(_MAC, occupied=False)
    assert await mgr.recover_unresponsive() == (_MAC,)
    assert device.unresponsive is False
    assert device.occupancy_state == "clear"
    assert mgr.devices == [device]
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.REAPPEARED, _MAC)


@pytest.mark.asyncio
async def test_recovery_follows_a_sensor_to_its_arp_address_and_updates_the_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda mac: _MOVED_HOST)
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    del lan.clients[_HOST]
    lan.clients[_MOVED_HOST] = _client(_MAC)
    assert await mgr.recover_unresponsive() == (_MAC,)
    assert device.host == _MOVED_HOST
    assert [(host, mac) for host, _port, mac, _name in load_ep1_devices(tmp_path / "cache.sqlite")] == [
        (_MOVED_HOST, _MAC)
    ]


@pytest.mark.asyncio
async def test_recovery_leaves_a_silent_sensor_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda mac: None)
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    notices_before = store.notices()
    assert await mgr.recover_unresponsive() == ()
    assert device.unresponsive is True
    assert store.notices() == notices_before


@pytest.mark.asyncio
async def test_recovery_rejects_another_sensor_at_the_address(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda mac: None)
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    lan.clients[_HOST] = _client(_OTHER_MAC)
    assert await mgr.recover_unresponsive() == ()
    assert device.unresponsive is True
    assert device.host == _HOST


@pytest.mark.asyncio
async def test_recovery_does_nothing_when_no_sensor_is_unresponsive(tmp_path: Path) -> None:
    lan = _Lan()
    mgr, _device = await _running_manager(tmp_path, DevicePresenceStore(), lan)
    lan.hosts_tried.clear()
    assert await mgr.recover_unresponsive() == ()
    assert lan.hosts_tried == []


@pytest.mark.asyncio
async def test_recovery_keeps_the_stub_when_the_cache_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    lan.clients[_HOST] = _client(_MAC)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("database is locked")

    monkeypatch.setattr("app.ep1_device_manager.device_discovery_store.upsert_ep1_device", _boom)
    assert await mgr.recover_unresponsive() == ()
    assert device.unresponsive is True


@pytest.mark.asyncio
async def test_recovery_sweep_finds_a_sensor_through_mdns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda mac: None)
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    del lan.clients[_HOST]
    lan.clients[_MOVED_HOST] = _client(_MAC)
    mgr._zeroconf_discovery = True
    mgr._zeroconf_discover_fn = AsyncMock(return_value=[(_MOVED_HOST, 6053)])
    assert await mgr.recover_unresponsive() == ()
    mgr._zeroconf_discover_fn.assert_not_awaited()
    assert await mgr.recover_unresponsive(sweep=True) == (_MAC,)
    assert device.host == _MOVED_HOST


@pytest.mark.asyncio
async def test_recovery_sweep_connects_to_each_discovered_host_once(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    silent = Ep1Device(_OTHER_MAC, host="192.0.2.20", mac_address=_OTHER_MAC)
    silent.set_unresponsive(True)
    mgr._devices[_OTHER_MAC] = silent
    del lan.clients[_HOST]
    lan.clients[_MOVED_HOST] = _client(_MAC)
    mgr._zeroconf_discovery = True
    mgr._zeroconf_discover_fn = AsyncMock(return_value=[(_MOVED_HOST, 6053)])
    lan.hosts_tried.clear()
    assert await mgr.recover_unresponsive(sweep=True) == (_MAC,)
    assert lan.hosts_tried.count(_MOVED_HOST) == 1
    assert device.host == _MOVED_HOST


@pytest.mark.asyncio
async def test_forget_removes_the_sensor_its_cache_row_and_its_notice(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, _device = await _flagged(tmp_path, store, lan)
    assert len(store.notices()) == 1
    assert await mgr.forget_device(_MAC.upper()) is True
    assert mgr.devices == []
    assert load_ep1_devices(tmp_path / "cache.sqlite") == []
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_forget_keeps_the_other_sensors(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, _device = await _running_manager(tmp_path, store, lan)
    other = Ep1Device(_OTHER_MAC, host="192.0.2.20", mac_address=_OTHER_MAC)
    mgr._devices[_OTHER_MAC] = other
    upsert_ep1_device(tmp_path / "cache.sqlite", host="192.0.2.20", port=6053, mac=_OTHER_MAC, friendly_name=None)
    assert await mgr.forget_device(_MAC) is True
    assert mgr.devices == [other]
    assert [mac for _host, _port, mac, _name in load_ep1_devices(tmp_path / "cache.sqlite")] == [_OTHER_MAC]


@pytest.mark.asyncio
async def test_forget_of_an_unknown_sensor_changes_nothing(tmp_path: Path) -> None:
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, DevicePresenceStore(), lan)
    assert await mgr.forget_device(_OTHER_MAC) is False
    assert await mgr.forget_device("not-a-mac") is False
    assert mgr.devices == [device]
    assert len(load_ep1_devices(tmp_path / "cache.sqlite")) == 1


@pytest.mark.asyncio
async def test_forget_keeps_the_sensor_when_the_cache_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)

    def _boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("database is locked")

    monkeypatch.setattr("app.ep1_device_manager.device_discovery_store.delete_ep1_device", _boom)
    with pytest.raises(OSError):
        await mgr.forget_device(_MAC)
    assert mgr.devices == [device]
    assert len(store.notices()) == 1


@pytest.mark.asyncio
async def test_a_forgotten_sensor_is_no_longer_owned_and_its_session_does_not_announce(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, store, lan)
    assert mgr.owns(device) is True
    await mgr.forget_device(_MAC)
    assert mgr.owns(device) is False
    assert mgr.forgotten(device) is True
    await _session(mgr, device)
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_a_state_pushed_after_the_sensor_was_forgotten_is_ignored(tmp_path: Path) -> None:
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, DevicePresenceStore(), lan)
    client = _client(_MAC)
    subscribed = asyncio.Event()
    callbacks: list[Any] = []

    def _subscribe(on_state: Any) -> None:
        callbacks.append(on_state)
        subscribed.set()

    client.subscribe_states = MagicMock(side_effect=_subscribe)
    lan.clients[_HOST] = client
    readings: list[object] = []
    stop = asyncio.Event()
    session = asyncio.create_task(
        mgr.run_subscription_session(device, stop=stop, on_reading_updated=lambda *_a: readings.append(_a))
    )
    await asyncio.wait_for(subscribed.wait(), timeout=5.0)
    callbacks[0](BinarySensorState(key=1, state=True))
    assert device.occupancy_state == "occupied"
    assert len(readings) == 1
    await mgr.forget_device(_MAC)
    callbacks[0](BinarySensorState(key=1, state=False))
    assert device.occupancy_state == "occupied"
    assert len(readings) == 1
    stop.set()
    await asyncio.wait_for(session, timeout=5.0)


@pytest.mark.asyncio
async def test_a_sensor_the_rediscover_missed_is_kept_and_not_forgotten(tmp_path: Path) -> None:
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, DevicePresenceStore(), lan)
    del lan.clients[_HOST]
    await mgr.rediscover()
    assert [(d.mac_address, d.unresponsive) for d in mgr.devices] == [(_MAC, True)]
    assert mgr.forgotten(device) is False


@pytest.mark.asyncio
async def test_a_forgotten_sensor_that_is_discovered_again_is_no_longer_forgotten(tmp_path: Path) -> None:
    lan = _Lan()
    mgr, device = await _running_manager(tmp_path, DevicePresenceStore(), lan)
    await mgr.forget_device(_MAC)
    assert mgr.forgotten(device) is True
    await mgr.rediscover(hosts=[(_HOST, 6053)])
    assert mgr.forgotten(device) is False


@pytest.mark.asyncio
async def test_a_failed_mdns_sweep_still_commits_the_sensors_the_direct_probes_recovered(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, device = await _flagged(tmp_path, store, lan)
    silent = Ep1Device(_OTHER_MAC, host="192.0.2.20", mac_address=_OTHER_MAC)
    silent.set_unresponsive(True)
    mgr._devices[_OTHER_MAC] = silent
    lan.clients[_HOST] = _client(_MAC)
    mgr._zeroconf_discovery = True
    mgr._zeroconf_discover_fn = AsyncMock(side_effect=OSError("network is down"))
    assert await mgr.recover_unresponsive(sweep=True) == (_MAC,)
    assert device.unresponsive is False
    assert silent.unresponsive is True


@pytest.mark.asyncio
async def test_fetch_announces_a_cached_sensor_that_only_arp_sees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    monkeypatch.setattr("app.ep1_device_manager.mac_alive_on_lan", lambda **_k: True)
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda _mac: _HOST)
    store = DevicePresenceStore()
    mgr = Ep1DeviceManager(
        discovery_cache_path=cache,
        force_discovery=True,
        noise_psk=None,
        presence_store=store,
        zeroconf_discovery=False,
        api_client_factory=lambda host, port, **_k: _client(_MAC, fail=True),
    )
    await mgr.fetch()
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.DISAPPEARED, _MAC)


def _sweeping_manager(cache: Path, store: DevicePresenceStore, lan: _Lan, **kwargs: Any) -> Ep1DeviceManager:
    return Ep1DeviceManager(
        discovery_cache_path=cache,
        force_discovery=True,
        noise_psk=None,
        presence_store=store,
        zeroconf_discovery=False,
        api_client_factory=lan.factory,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_sweep_keeps_a_cached_sensor_that_answers_neither_the_api_nor_arp(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    store = DevicePresenceStore()
    mgr = _sweeping_manager(cache, store, _Lan())
    await mgr.fetch()
    (device,) = mgr.devices
    assert (device.mac_address, device.host, device.unresponsive) == (_MAC, _HOST, True)
    assert device.preferred_label == "Office"
    assert load_ep1_devices(cache) == [(_HOST, 6053, _MAC, "Office")]
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.DISAPPEARED, _MAC)


@pytest.mark.asyncio
async def test_rediscover_keeps_a_silent_sensor_and_lists_it_again_when_it_answers(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    lan = _Lan()
    mgr, _device = await _running_manager(tmp_path, store, lan)
    cache = tmp_path / "cache.sqlite"
    del lan.clients[_HOST]
    await mgr.rediscover()
    (kept,) = mgr.devices
    assert (kept.mac_address, kept.unresponsive) == (_MAC, True)
    assert load_ep1_devices(cache) == [(_HOST, 6053, _MAC, "Office")]
    lan.clients[_HOST] = _client(_MAC)
    assert await mgr.recover_unresponsive() == (_MAC,)
    (back,) = mgr.devices
    assert back.unresponsive is False
    assert {notice.kind for notice in store.notices()} == {DevicePresenceKind.REAPPEARED}


@pytest.mark.asyncio
async def test_sweep_drops_a_silent_sensor_whose_address_now_belongs_to_another_sensor(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    lan = _Lan()
    lan.clients[_HOST] = _client(_OTHER_MAC, name="Hall")
    mgr = _sweeping_manager(cache, DevicePresenceStore(), lan, configured_hosts=[(_HOST, 6053)])
    with caplog.at_level("WARNING"):
        await mgr.fetch()
    assert [device.mac_address for device in mgr.devices] == [_OTHER_MAC]
    assert load_ep1_devices(cache) == [(_HOST, 6053, _OTHER_MAC, "Hall")]
    assert any(_MAC in record.getMessage() for record in caplog.records if record.levelname == "WARNING")


@pytest.mark.asyncio
async def test_forgetting_a_kept_sensor_removes_it_and_its_cache_row(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    mgr = _sweeping_manager(cache, DevicePresenceStore(), _Lan())
    await mgr.fetch()
    assert await mgr.forget_device(_MAC) is True
    assert mgr.devices == []
    assert load_ep1_devices(cache) == []


@pytest.mark.asyncio
async def test_sweep_keeps_a_silent_sensor_that_moved_while_another_sensor_took_its_old_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    monkeypatch.setattr(
        "app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda mac: _MOVED_HOST if mac == _MAC else None
    )
    lan = _Lan()
    lan.clients[_HOST] = _client(_OTHER_MAC, name="Hall")
    mgr = _sweeping_manager(cache, DevicePresenceStore(), lan, configured_hosts=[(_HOST, 6053)])
    await mgr.fetch()
    assert {d.mac_address: (d.host, d.unresponsive) for d in mgr.devices} == {
        _MAC: (_MOVED_HOST, True),
        _OTHER_MAC: (_HOST, False),
    }
    assert sorted(row[0] for row in load_ep1_devices(cache)) == sorted([_HOST, _MOVED_HOST])


@pytest.mark.asyncio
async def test_cache_first_startup_lists_a_powered_off_sensor_as_unresponsive(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(cache, host=_HOST, port=6053, mac=_MAC, friendly_name="Office")
    store = DevicePresenceStore()
    mgr = Ep1DeviceManager(
        discovery_cache_path=cache,
        noise_psk=None,
        presence_store=store,
        zeroconf_discovery=False,
        api_client_factory=_Lan().factory,
    )
    await mgr.fetch()
    (device,) = mgr.devices
    assert (device.mac_address, device.host, device.unresponsive) == (_MAC, _HOST, True)
    assert mgr.last_discovery_source == "cache"
    assert load_ep1_devices(cache) == [(_HOST, 6053, _MAC, "Office")]
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.DISAPPEARED, _MAC)
