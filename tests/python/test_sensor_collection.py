"""Hermetic tests for the sensor collection sampler."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    SensorChartWindow,
    SensorCollectionKey,
)
from app.sensor_collection import run_sensor_collection_sampler
from app.sensor_collection_store import (
    insert_sensor_sample,
    latest_sensor_sample,
    list_sensor_samples,
    save_sensor_collection_config,
)


def _ep1_device(
    *,
    device_id: str = "aa:bb:cc:dd:ee:ff",
    occupancy: str = DeviceConditionState.OCCUPIED.value,
    temperature_c: float = 21.5,
) -> MagicMock:
    device = MagicMock()
    device.identifier = device_id
    device.mac_address = device_id
    device.preferred_label = "Office"
    device.occupancy_state = occupancy
    device.temperature_c = temperature_c
    device.humidity_pct = 45.0
    device.illuminance_lx = 120.0
    return device


@pytest.mark.asyncio
async def test_sampler_writes_enabled_sensor_and_respects_interval(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    save_sensor_collection_config(
        db,
        device_id=device_id,
        enabled=True,
        family_id=DeviceFamilyId.EP1.value,
        interval_s=30,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
    )
    save_sensor_collection_config(
        db,
        device_id=device_id,
        enabled=False,
        family_id=DeviceFamilyId.EP1.value,
        interval_s=5,
        sensor_key=SensorCollectionKey.OCCUPANCY,
    )

    state = MagicMock()
    state.ep1_mgr = MagicMock()
    state.ep1_mgr.devices = [_ep1_device(device_id=device_id)]

    clock = {"t": 1_000.0}
    stop = asyncio.Event()

    async def _run() -> None:
        await run_sensor_collection_sampler(
            cache_path=db,
            device_state_getter=lambda: state,
            stop=stop,
            sleep_s=0.01,
            time_fn=lambda: clock["t"],
        )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    first = latest_sensor_sample(
        db,
        device_id=device_id,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
    )
    assert first is not None
    assert first.value == 21.5

    clock["t"] = 1_010.0
    await asyncio.sleep(0.05)
    still_one = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
        window=SensorChartWindow.LAST_HOUR,
        now=clock["t"],
    )
    assert len(still_one) == 1

    clock["t"] = 1_040.0
    await asyncio.sleep(0.05)
    two = list_sensor_samples(
        db,
        device_id=device_id,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
        window=SensorChartWindow.LAST_HOUR,
        now=clock["t"],
    )
    assert len(two) == 2

    occupancy = latest_sensor_sample(
        db,
        device_id=device_id,
        sensor_key=SensorCollectionKey.OCCUPANCY,
    )
    assert occupancy is None

    stop.set()
    await task


@pytest.mark.asyncio
async def test_sampler_honors_interval_when_reading_unavailable(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    save_sensor_collection_config(
        db,
        device_id=device_id,
        enabled=True,
        family_id=DeviceFamilyId.EP1.value,
        interval_s=30,
        sensor_key=SensorCollectionKey.OCCUPANCY,
    )
    state = MagicMock()
    state.ep1_mgr = MagicMock()
    state.ep1_mgr.devices = [_ep1_device(device_id=device_id)]
    clock = {"t": 1_000.0}
    stop = asyncio.Event()
    read_calls = {"n": 0}

    def _counting_read(*args: object, **kwargs: object) -> None:
        read_calls["n"] += 1
        return None

    async def _run() -> None:
        await run_sensor_collection_sampler(
            cache_path=db,
            device_state_getter=lambda: state,
            stop=stop,
            sleep_s=0.01,
            time_fn=lambda: clock["t"],
        )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.sensor_collection.read_sensor_value", _counting_read)
        task = asyncio.create_task(_run())
        await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        assert read_calls["n"] == 1
        await asyncio.sleep(0.08)
        assert read_calls["n"] == 1
        clock["t"] = 1_040.0
        await asyncio.sleep(0.05)
        assert read_calls["n"] == 2
        stop.set()
        await task


@pytest.mark.asyncio
async def test_sampler_prunes_stale_samples_without_enabled_sensors(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    device_id = "aa:bb:cc:dd:ee:ff"
    recorded_at = 1_000.0
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id=DeviceFamilyId.EP1.value,
        recorded_at=recorded_at,
        sensor_key=SensorCollectionKey.TEMPERATURE_C,
        unit="°C",
        value=21.5,
        now=recorded_at,
    )
    clock = {"t": recorded_at + 61 * 86_400.0}
    stop = asyncio.Event()

    async def _run() -> None:
        await run_sensor_collection_sampler(
            cache_path=db,
            device_state_getter=lambda: None,
            stop=stop,
            sleep_s=0.01,
            time_fn=lambda: clock["t"],
        )

    task = asyncio.create_task(_run())
    await asyncio.sleep(0)
    await asyncio.sleep(0.05)
    assert (
        latest_sensor_sample(
            db,
            device_id=device_id,
            sensor_key=SensorCollectionKey.TEMPERATURE_C,
        )
        is None
    )
    stop.set()
    await task
