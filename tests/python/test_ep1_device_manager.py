# pyright: reportCallIssue=false
"""Hermetic tests for Ep1DeviceManager with a mocked aioesphomeapi client."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aioesphomeapi.model import (
    BinarySensorInfo,
    BinarySensorState,
    SensorInfo,
    SensorState,
)

from app.device_discovery_store import load_ep1_devices, upsert_ep1_device
from app.device_enums import DeviceConditionState, Ep1EntityRole
from app.ep1_device_manager import Ep1Device, Ep1DeviceManager, Ep1DiscoveryError


@pytest.mark.asyncio
async def test_fetch_connects_reads_entities_and_caches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")

    entities = [
        BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy"),
        SensorInfo(object_id="temperature_sensor", key=2, name="Temperature"),
        SensorInfo(object_id="humidity_sensor", key=3, name="Humidity"),
        SensorInfo(object_id="illuminance_sensor", key=4, name="Illuminance"),
    ]
    states = [
        BinarySensorState(key=1, state=True),
        SensorState(key=2, state=21.5),
        SensorState(key=3, state=40.0),
        SensorState(key=4, state=120.0),
    ]

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:FF"
    info.friendly_name = "Office EP1"
    info.name = "office-ep1"

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(return_value=(entities, []))

    def _subscribe(on_state: Any) -> None:
        for state in states:
            on_state(state)

    client.subscribe_states = MagicMock(side_effect=_subscribe)

    def _factory(*_a: Any, **_k: Any) -> MagicMock:
        return client

    cache = tmp_path / "cache.sqlite"
    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=cache,
        api_client_factory=_factory,
    )
    await mgr.fetch()

    devices = mgr.devices
    assert len(devices) == 1
    device = devices[0]
    assert device.identifier == "aa:bb:cc:dd:ee:ff"
    assert device.occupancy_state == DeviceConditionState.OCCUPIED.value
    assert device.temperature_c == 21.5
    assert device.humidity_pct == 40.0
    assert device.illuminance_lx == 120.0
    assert mgr.last_discovery_source == "discovery"

    rows = load_ep1_devices(cache)
    assert rows == [("192.0.2.10", 6053, "aa:bb:cc:dd:ee:ff", "Office EP1")]
    client.disconnect.assert_awaited()
    assert mgr._clients == []

    await mgr.disconnect()


@pytest.mark.asyncio
async def test_force_discovery_falls_back_to_cache_when_no_lan_targets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:01"
    info.friendly_name = "Cached"
    info.name = "cached"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=False)))
    called_hosts: list[tuple[str, int]] = []

    def _factory(host: str, port: int, **_k: Any) -> MagicMock:
        called_hosts.append((host, port))
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=cache,
        force_discovery=True,
        zeroconf_discovery=False,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert called_hosts == [("192.0.2.99", 6053)]
    assert [d.host for d in mgr.devices] == ["192.0.2.99"]
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_force_discovery_keeps_arp_visible_cached_ep1_as_unresponsive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )
    client = MagicMock()
    client.connect = AsyncMock(side_effect=RuntimeError("timeout"))
    client.disconnect = AsyncMock()
    monkeypatch.setattr("app.ep1_device_manager.mac_alive_on_lan", lambda **_k: True)
    monkeypatch.setattr("app.ep1_device_manager.lookup_ip_via_arp_for_mac", lambda _mac: "192.0.2.99")
    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=cache,
        force_discovery=True,
        zeroconf_discovery=False,
        api_client_factory=lambda host, port, **_k: client,
    )
    await mgr.fetch()
    assert len(mgr.devices) == 1
    device = mgr.devices[0]
    assert device.mac_address == "aa:bb:cc:dd:ee:01"
    assert device.unresponsive is True
    assert device.occupancy_state == "unknown"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_force_discovery_prefers_configured_hosts_over_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:22"
    info.friendly_name = "Configured"
    info.name = "configured"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=True)))
    called_hosts: list[tuple[str, int]] = []

    def _factory(host: str, port: int, **_k: Any) -> MagicMock:
        called_hosts.append((host, port))
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=cache,
        force_discovery=True,
        zeroconf_discovery=False,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert called_hosts == [("192.0.2.10", 6053)]
    assert [d.host for d in mgr.devices] == ["192.0.2.10"]
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_connect_discards_client_when_mac_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")

    info = MagicMock()
    info.mac_address = ""
    info.friendly_name = "No MAC"
    info.name = "no-mac"

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)

    def _factory(*_a: Any, **_k: Any) -> MagicMock:
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert mgr.devices == []
    client.disconnect.assert_awaited()
    assert mgr._clients == []
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_run_subscription_session_applies_states_until_stop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")

    entities = [
        BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy"),
        SensorInfo(object_id="temperature_sensor", key=2, name="Temperature"),
    ]
    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:FF"
    info.friendly_name = "Office EP1"
    info.name = "office-ep1"

    on_state_cb: list[Any] = []
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(return_value=(entities, []))

    def _subscribe(on_state: Any) -> None:
        on_state_cb.append(on_state)

    client.subscribe_states = MagicMock(side_effect=_subscribe)

    def _factory(*_a: Any, **_k: Any) -> MagicMock:
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        noise_psk="test-psk",
        api_client_factory=_factory,
    )
    device = Ep1Device(
        "aa:bb:cc:dd:ee:ff",
        display_name="Office EP1",
        host="192.0.2.10",
        port=6053,
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    mgr._devices[device.identifier] = device
    mgr._fetched = True

    stop = asyncio.Event()
    updated: list[tuple[str, Ep1EntityRole]] = []

    async def _run() -> None:
        await mgr.run_subscription_session(
            device,
            stop=stop,
            on_reading_updated=lambda d, role: updated.append((d.identifier, role)),
        )

    task = asyncio.create_task(_run())
    for _ in range(50):
        if on_state_cb:
            break
        await asyncio.sleep(0.01)
    assert on_state_cb
    on_state_cb[0](BinarySensorState(key=1, state=True))
    on_state_cb[0](SensorState(key=2, state=22.0))
    assert device.occupancy_state == DeviceConditionState.OCCUPIED.value
    assert device.temperature_c == 22.0
    assert device.last_heard_at is not None
    assert updated == [
        (device.identifier, Ep1EntityRole.OCCUPANCY),
        (device.identifier, Ep1EntityRole.TEMPERATURE),
    ]
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    client.disconnect.assert_awaited()


@pytest.mark.asyncio
async def test_run_subscription_session_notes_heard_after_subscribe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")

    entities = [
        BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy"),
    ]
    on_state_cb: list[Any] = []
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=(entities, []))

    def _subscribe(on_state: Any) -> None:
        on_state_cb.append(on_state)

    client.subscribe_states = MagicMock(side_effect=_subscribe)

    def _factory(*_a: Any, **_k: Any) -> MagicMock:
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        noise_psk="test-psk",
        api_client_factory=_factory,
    )
    device = Ep1Device(
        "aa:bb:cc:dd:ee:ff",
        display_name="Office EP1",
        host="192.0.2.10",
        port=6053,
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    mgr._devices[device.identifier] = device
    mgr._fetched = True

    stop = asyncio.Event()

    async def _run() -> None:
        await mgr.run_subscription_session(device, stop=stop)

    task = asyncio.create_task(_run())
    for _ in range(50):
        if on_state_cb and device.last_heard_at is not None:
            break
        await asyncio.sleep(0.01)
    assert on_state_cb, "subscribe_states should register before note_heard"
    assert device.last_heard_at is not None
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)


@pytest.mark.asyncio
async def test_fetch_plaintext_without_noise_psk(tmp_path: Path) -> None:
    entities = [
        BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy"),
        SensorInfo(object_id="temperature", key=2, name="Temperature"),
        SensorInfo(object_id="humidity", key=3, name="Humidity"),
        SensorInfo(object_id="illuminance", key=4, name="Illuminance"),
    ]
    states = [
        BinarySensorState(key=1, state=False),
        SensorState(key=2, state=23.5),
        SensorState(key=3, state=55.0),
        SensorState(key=4, state=10.0),
    ]
    info = MagicMock()
    info.mac_address = "28:05:A5:28:C8:48"
    info.friendly_name = "Everything Presence One 28c848"
    info.name = "everything-presence-one-28c848"

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(return_value=(entities, []))

    def _subscribe(on_state: Any) -> None:
        for state in states:
            on_state(state)

    client.subscribe_states = MagicMock(side_effect=_subscribe)
    called: list[dict[str, Any]] = []

    def _factory(*_a: Any, **kwargs: Any) -> MagicMock:
        called.append(kwargs)
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        noise_psk=None,
        zeroconf_discovery=False,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert len(mgr.devices) == 1
    device = mgr.devices[0]
    assert device.temperature_c == 23.5
    assert device.humidity_pct == 55.0
    assert device.illuminance_lx == 10.0
    assert device.occupancy_state == DeviceConditionState.CLEAR.value
    assert called and called[0].get("noise_psk") is None
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_fetch_uses_zeroconf_when_no_hosts(tmp_path: Path) -> None:
    async def _fake_discover(*, timeout: float) -> list[tuple[str, int]]:
        assert timeout == 3.0
        return [("192.0.2.77", 6053)]

    entities = [BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")]
    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:11"
    info.friendly_name = "EP1 Zeroconf"
    info.name = "ep1"

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(return_value=(entities, []))
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=True)))

    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=tmp_path / "cache.sqlite",
        noise_psk=None,
        zeroconf_timeout=3.0,
        zeroconf_discover_fn=_fake_discover,
        api_client_factory=lambda *_a, **_k: client,
    )
    await mgr.fetch()
    assert len(mgr.devices) == 1
    assert mgr.devices[0].host == "192.0.2.77"
    assert mgr.last_discovery_source == "discovery"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_uses_hosts_override_and_cache_fallback(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:22"
    info.friendly_name = "Override"
    info.name = "override"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=False)))
    called_hosts: list[tuple[str, int]] = []

    def _factory(host: str, port: int, **_k: Any) -> MagicMock:
        called_hosts.append((host, port))
        return client

    async def _no_mdns(*, timeout: float) -> list[tuple[str, int]]:
        del timeout
        raise Ep1DiscoveryError("none")

    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=cache,
        noise_psk=None,
        zeroconf_discovery=True,
        zeroconf_discover_fn=_no_mdns,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert [d.host for d in mgr.devices] == ["192.0.2.99"]

    called_hosts.clear()
    await mgr.rediscover(hosts=[("192.0.2.55", 6053)])
    assert called_hosts == [("192.0.2.55", 6053)]
    assert [d.host for d in mgr.devices] == ["192.0.2.55"]

    called_hosts.clear()
    await mgr.rediscover()
    # mDNS fails → fall back to cache (may include newly upserted 192.0.2.55)
    assert called_hosts
    assert mgr.devices
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_restores_devices_when_fetch_raises(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:01"
    info.friendly_name = "Cached"
    info.name = "cached"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=True)))

    async def _boom(*, timeout: float) -> list[tuple[str, int]]:
        del timeout
        raise RuntimeError("zeroconf exploded")

    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=cache,
        noise_psk=None,
        zeroconf_discovery=True,
        zeroconf_discover_fn=_boom,
        api_client_factory=lambda *_a, **_k: client,
    )
    await mgr.fetch()
    assert [d.host for d in mgr.devices] == ["192.0.2.99"]
    before = mgr.devices[0]

    with pytest.raises(RuntimeError, match="zeroconf exploded"):
        await mgr.rediscover()

    assert mgr.devices[0] is before
    assert mgr.devices[0].host == "192.0.2.99"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_clears_roster_when_all_connects_fail(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    upsert_ep1_device(
        cache,
        host="192.0.2.99",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Cached",
    )

    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:01"
    info.friendly_name = "Cached"
    info.name = "cached"
    good_client = MagicMock()
    good_client.connect = AsyncMock()
    good_client.disconnect = AsyncMock()
    good_client.device_info = AsyncMock(return_value=info)
    good_client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    good_client.subscribe_states = MagicMock(
        side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=True))
    )

    bad_client = MagicMock()
    bad_client.connect = AsyncMock(side_effect=RuntimeError("unreachable"))
    bad_client.disconnect = AsyncMock()

    call_n = {"n": 0}

    def _factory(*_a: Any, **_k: Any) -> MagicMock:
        call_n["n"] += 1
        return good_client if call_n["n"] == 1 else bad_client

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.99", 6053)],
        discovery_cache_path=cache,
        noise_psk=None,
        zeroconf_discovery=False,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert mgr.devices[0].host == "192.0.2.99"

    await mgr.rediscover(hosts=[("192.0.2.55", 6053)])
    assert mgr.devices == []
    assert load_ep1_devices(cache) == []
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_drops_missing_sensor_when_subset_reconnects(tmp_path: Path) -> None:
    cache = tmp_path / "cache.sqlite"
    for host, mac, name in (
        ("192.0.2.10", "aa:bb:cc:dd:ee:01", "Office"),
        ("192.0.2.20", "aa:bb:cc:dd:ee:02", "Hall"),
    ):
        upsert_ep1_device(cache, host=host, port=6053, mac=mac, friendly_name=name)

    def _info(mac: str, name: str) -> MagicMock:
        info = MagicMock()
        info.mac_address = mac
        info.friendly_name = name
        info.name = name.lower()
        return info

    clients: dict[str, MagicMock] = {}
    for host, mac, name in (
        ("192.0.2.10", "AA:BB:CC:DD:EE:01", "Office"),
        ("192.0.2.20", "AA:BB:CC:DD:EE:02", "Hall"),
    ):
        client = MagicMock()
        client.connect = AsyncMock()
        client.disconnect = AsyncMock()
        client.device_info = AsyncMock(return_value=_info(mac, name))
        client.list_entities_services = AsyncMock(
            return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
        )
        client.subscribe_states = MagicMock(
            side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=False))
        )
        clients[host] = client

    def _factory(host: str, port: int, **_k: Any) -> MagicMock:
        del port
        return clients[host]

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053), ("192.0.2.20", 6053)],
        discovery_cache_path=cache,
        noise_psk=None,
        zeroconf_discovery=False,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert sorted(d.host for d in mgr.devices) == ["192.0.2.10", "192.0.2.20"]

    clients["192.0.2.20"].connect = AsyncMock(side_effect=RuntimeError("hall offline"))
    await mgr.rediscover()
    assert [d.host for d in mgr.devices] == ["192.0.2.10"]
    rows = load_ep1_devices(cache)
    assert len(rows) == 1
    assert rows[0][0] == "192.0.2.10"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_refresh_device_readings_updates_in_place(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")
    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:FF"
    info.friendly_name = "Office EP1"
    info.name = "office"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=(
            [
                BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy"),
                SensorInfo(object_id="temperature", key=2, name="Temperature", device_class="temperature"),
            ],
            [],
        )
    )

    def _subscribe(on_state: Any) -> None:
        on_state(BinarySensorState(key=1, state=True))
        on_state(SensorState(key=2, state=22.25))

    client.subscribe_states = MagicMock(side_effect=_subscribe)

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        api_client_factory=lambda *_a, **_k: client,
    )
    await mgr.fetch()
    device = mgr.devices[0]
    assert device.temperature_c == 22.25

    def _subscribe_refresh(on_state: Any) -> None:
        on_state(BinarySensorState(key=1, state=False))
        on_state(SensorState(key=2, state=19.5))

    client.subscribe_states = MagicMock(side_effect=_subscribe_refresh)
    await mgr.refresh_device_readings(device.identifier)
    assert mgr.devices[0] is device
    assert device.occupancy_state == DeviceConditionState.CLEAR.value
    assert device.temperature_c == 19.5
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_refresh_device_readings_rejects_empty_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "test-psk")
    info = MagicMock()
    info.mac_address = "AA:BB:CC:DD:EE:FF"
    info.friendly_name = "Office EP1"
    info.name = "office"
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.device_info = AsyncMock(return_value=info)
    client.list_entities_services = AsyncMock(
        return_value=([BinarySensorInfo(object_id="occupancy", key=1, name="Occupancy")], [])
    )
    client.subscribe_states = MagicMock(side_effect=lambda on_state: on_state(BinarySensorState(key=1, state=True)))

    mgr = Ep1DeviceManager(
        configured_hosts=[("192.0.2.10", 6053)],
        discovery_cache_path=tmp_path / "cache.sqlite",
        api_client_factory=lambda *_a, **_k: client,
    )
    await mgr.fetch()
    device = mgr.devices[0]
    before_updated = device.readings_updated_at

    client.subscribe_states = MagicMock(side_effect=lambda _on_state: None)
    with pytest.raises(RuntimeError, match="collected no occupancy reading"):
        await mgr.refresh_device_readings(device.identifier)

    assert device.occupancy_state == DeviceConditionState.OCCUPIED.value
    assert device.readings_updated_at == before_updated
    await mgr.disconnect()
