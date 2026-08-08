"""Automations → Data: catalog collectible sensors and run the sampler loop."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from app.device_display import format_device_display
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    SensorCollectionKey,
)
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1Device
from app.sensor_collection_store import (
    DEFAULT_SENSOR_COLLECTION_INTERVAL_S,
    SensorCollectionConfigRecord,
    insert_sensor_sample,
    latest_sensor_sample,
    list_sensor_collection_configs,
    prune_sensor_samples,
)

_LOGGER = logging.getLogger(__name__)

_PRUNE_INTERVAL_S = 3600.0
_SAMPLER_IDLE_POLL_S = 1.0


@dataclass(frozen=True)
class CollectibleSensor:
    """One reading an operator can enable for collection."""

    device_id: str
    display_name: str
    family_id: DeviceFamilyId
    sensor_key: SensorCollectionKey


@dataclass(frozen=True)
class SensorCollectionRow:
    """Catalog row merged with persisted config and latest sample."""

    device_display: str
    device_id: str
    display_name: str
    enabled: bool
    family_id: DeviceFamilyId
    interval_s: int
    last_sample_at: float | None
    last_value: float | None
    sensor_key: SensorCollectionKey
    unit: str | None


def build_sensor_collection_rows(
    state: DeviceManagersState | None,
    path: Path,
) -> list[SensorCollectionRow]:
    """Merge live collectible sensors with SQLite config + latest samples."""
    configs = {(row.device_id, row.sensor_key): row for row in list_sensor_collection_configs(path)}
    out: list[SensorCollectionRow] = []
    seen: set[tuple[str, SensorCollectionKey]] = set()
    for sensor in list_collectible_sensors(state):
        key = (sensor.device_id, sensor.sensor_key)
        seen.add(key)
        config = configs.get(key)
        latest = latest_sensor_sample(
            path,
            device_id=sensor.device_id,
            sensor_key=sensor.sensor_key,
        )
        enabled = config.enabled if config is not None else False
        interval_s = config.interval_s if config is not None else DEFAULT_SENSOR_COLLECTION_INTERVAL_S
        out.append(
            SensorCollectionRow(
                device_display=format_device_display(sensor.device_id, sensor.display_name),
                device_id=sensor.device_id,
                display_name=sensor.display_name,
                enabled=enabled,
                family_id=sensor.family_id,
                interval_s=interval_s,
                last_sample_at=None if latest is None else latest.recorded_at,
                last_value=None if latest is None else latest.value,
                sensor_key=sensor.sensor_key,
                unit=sensor.sensor_key.unit_label(),
            )
        )
    for key, config in configs.items():
        if key in seen:
            continue
        device_id, sensor_key = key
        latest = latest_sensor_sample(
            path,
            device_id=device_id,
            sensor_key=sensor_key,
        )
        try:
            family_id = DeviceFamilyId(config.family_id)
        except ValueError:
            family_id = DeviceFamilyId.EP1
        out.append(
            SensorCollectionRow(
                device_display=format_device_display(device_id, device_id),
                device_id=device_id,
                display_name=device_id,
                enabled=config.enabled,
                family_id=family_id,
                interval_s=config.interval_s,
                last_sample_at=None if latest is None else latest.recorded_at,
                last_value=None if latest is None else latest.value,
                sensor_key=sensor_key,
                unit=sensor_key.unit_label(),
            )
        )
    out.sort(key=lambda row: (row.device_display.lower(), row.sensor_key.value))
    return out


def list_collectible_sensors(state: DeviceManagersState | None) -> list[CollectibleSensor]:
    """Return sensors already exposed by device managers (EP1 v1)."""
    if state is None or state.ep1_mgr is None:
        return []
    out: list[CollectibleSensor] = []
    for device in state.ep1_mgr.devices:
        device_id = (device.identifier or "").strip()
        if device_id == "":
            continue
        display_name = device.preferred_label
        for key in SensorCollectionKey:
            out.append(
                CollectibleSensor(
                    device_id=device_id,
                    display_name=display_name,
                    family_id=DeviceFamilyId.EP1,
                    sensor_key=key,
                )
            )
    return out


def read_sensor_value(
    state: DeviceManagersState,
    *,
    device_id: str,
    family_id: DeviceFamilyId,
    sensor_key: SensorCollectionKey,
) -> float | None:
    """Return the live numeric value for one sensor, or ``None`` when unknown."""
    if family_id != DeviceFamilyId.EP1 or state.ep1_mgr is None:
        return None
    device = _find_ep1(state, device_id)
    if device is None:
        return None
    return _ep1_sensor_value(device, sensor_key)


async def run_sensor_collection_sampler(
    *,
    cache_path: Path,
    device_state_getter: Callable[[], DeviceManagersState | None],
    stop: asyncio.Event,
    sleep_s: float = _SAMPLER_IDLE_POLL_S,
    time_fn: Callable[[], float] = time.time,
) -> None:
    """Background loop: sample enabled sensors at their configured intervals."""
    last_prune_at = 0.0
    last_sample_at = _seed_last_sample_at(cache_path)
    _LOGGER.info("[sensor-collection] sampler started")
    try:
        while not stop.is_set():
            now = time_fn()
            if now - last_prune_at >= _PRUNE_INTERVAL_S:
                try:
                    deleted = prune_sensor_samples(cache_path, now=now)
                    if deleted:
                        _LOGGER.info(
                            "[sensor-collection] pruned %s sample(s) past retention",
                            deleted,
                        )
                except Exception:
                    _LOGGER.exception("[sensor-collection] retention prune failed")
                last_prune_at = now
            try:
                _sample_due_sensors(
                    cache_path=cache_path,
                    device_state_getter=device_state_getter,
                    last_sample_at=last_sample_at,
                    now=now,
                )
            except Exception:
                _LOGGER.exception("[sensor-collection] sample cycle failed")
            try:
                await asyncio.wait_for(stop.wait(), timeout=sleep_s)
            except TimeoutError:
                continue
    finally:
        _LOGGER.info("[sensor-collection] sampler stopped")


def _ep1_sensor_value(device: Ep1Device, sensor_key: SensorCollectionKey) -> float | None:
    match sensor_key:
        case SensorCollectionKey.HUMIDITY_PCT:
            return device.humidity_pct
        case SensorCollectionKey.ILLUMINANCE_LX:
            return device.illuminance_lx
        case SensorCollectionKey.OCCUPANCY:
            occupancy = device.occupancy_state
            if occupancy == DeviceConditionState.OCCUPIED.value:
                return 1.0
            if occupancy == DeviceConditionState.CLEAR.value:
                return 0.0
            return None
        case SensorCollectionKey.TEMPERATURE_C:
            return device.temperature_c


def _find_ep1(state: DeviceManagersState, device_id: str) -> Ep1Device | None:
    mgr = state.ep1_mgr
    if mgr is None:
        return None
    needle = device_id.strip().casefold()
    for device in mgr.devices:
        identifier = (device.identifier or "").strip().casefold()
        mac = (device.mac_address or "").strip().casefold()
        if identifier == needle or mac == needle:
            return device
    return None


def _sample_due_sensors(
    *,
    cache_path: Path,
    device_state_getter: Callable[[], DeviceManagersState | None],
    last_sample_at: dict[tuple[str, SensorCollectionKey], float],
    now: float,
) -> None:
    state = device_state_getter()
    if state is None:
        return
    configs = [row for row in list_sensor_collection_configs(cache_path) if row.enabled]
    if not configs:
        return
    for config in configs:
        _maybe_sample_one(
            cache_path=cache_path,
            config=config,
            last_sample_at=last_sample_at,
            now=now,
            state=state,
        )


def _maybe_sample_one(
    *,
    cache_path: Path,
    config: SensorCollectionConfigRecord,
    last_sample_at: dict[tuple[str, SensorCollectionKey], float],
    now: float,
    state: DeviceManagersState,
) -> None:
    key = (config.device_id, config.sensor_key)
    previous = last_sample_at.get(key)
    if previous is not None and (now - previous) < config.interval_s:
        return
    try:
        family_id = DeviceFamilyId(config.family_id)
    except ValueError:
        _LOGGER.warning(
            "[sensor-collection] unknown family_id=%s for device_id=%s sensor_key=%s",
            config.family_id,
            config.device_id,
            config.sensor_key.value,
        )
        last_sample_at[key] = now
        return
    value = read_sensor_value(
        state,
        device_id=config.device_id,
        family_id=family_id,
        sensor_key=config.sensor_key,
    )
    if value is None:
        # Honor interval even when the reading is unavailable (unknown occupancy,
        # offline device, etc.) so we do not re-probe every idle poll tick.
        last_sample_at[key] = now
        return
    try:
        insert_sensor_sample(
            cache_path,
            device_id=config.device_id,
            family_id=config.family_id,
            recorded_at=now,
            sensor_key=config.sensor_key,
            unit=config.sensor_key.unit_label(),
            value=value,
            now=now,
        )
    except Exception:
        _LOGGER.exception(
            "[sensor-collection] insert failed device_id=%s sensor_key=%s",
            config.device_id,
            config.sensor_key.value,
        )
    last_sample_at[key] = now


def _seed_last_sample_at(
    cache_path: Path,
) -> dict[tuple[str, SensorCollectionKey], float]:
    """Seed interval clocks from the newest persisted sample per sensor."""
    seeded: dict[tuple[str, SensorCollectionKey], float] = {}
    for config in list_sensor_collection_configs(cache_path):
        latest = latest_sensor_sample(
            cache_path,
            device_id=config.device_id,
            sensor_key=config.sensor_key,
        )
        if latest is None:
            continue
        seeded[(config.device_id, config.sensor_key)] = latest.recorded_at
    return seeded
