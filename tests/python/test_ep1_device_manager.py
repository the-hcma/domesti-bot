# pyright: reportCallIssue=false
"""Hermetic tests for Ep1DeviceManager with a mocked aioesphomeapi client."""

from __future__ import annotations

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
from app.device_enums import DeviceConditionState
from app.ep1_device_manager import Ep1DeviceManager


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

    await mgr.disconnect()


@pytest.mark.asyncio
async def test_force_discovery_ignores_cache(
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
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    called_hosts: list[tuple[str, int]] = []

    def _factory(host: str, port: int, **_k: Any) -> MagicMock:
        called_hosts.append((host, port))
        return client

    mgr = Ep1DeviceManager(
        configured_hosts=[],
        discovery_cache_path=cache,
        force_discovery=True,
        api_client_factory=_factory,
    )
    await mgr.fetch()
    assert called_hosts == []
    assert mgr.devices == []
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
