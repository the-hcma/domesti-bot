"""EP1 climate / light calibration offsets via ESPHome ``number`` entities.

Stock Everything Presence One firmware exposes template numbers for temperature,
humidity, and illuminance offsets (``restore_value: true``). Settings reads and
writes them with an ephemeral :class:`~aioesphomeapi.client.APIClient` so the
live subscription watcher is not disrupted.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from aioesphomeapi.client import APIClient
from aioesphomeapi.core import APIConnectionError
from aioesphomeapi.model import (
    EntityInfo,
    EntityState,
    NumberInfo,
    NumberState,
    SensorInfo,
    SensorState,
)

from app import device_discovery_store
from app.device_display import format_device_display
from app.device_enums import Ep1CalibrationOffsetKind
from app.device_mac import try_normalize_mac
from app.device_manager import NotInitializedError
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import DEFAULT_EP1_API_PORT, Ep1Device, Ep1DeviceManager
from app.server_runtime import runtime

_LOGGER = logging.getLogger(__name__)

EP1_CALIBRATION_DEVICE_NOT_FOUND = "No EP1 device matched device_id={device_id!r}"
EP1_CALIBRATION_NO_DEVICES = "No EP1 devices discovered yet; run discovery or set EP1_HOSTS first"
EP1_CALIBRATION_OFFSET_UNAVAILABLE = (
    "EP1 at {host}:{port} has no {kind} offset number entity (expected object_id aliases {aliases})"
)
EP1_CALIBRATION_STATE_TIMEOUT = "Timed out waiting for EP1 calibration states at {host}:{port}"

_OFFSET_ENTITY_ALIASES: dict[Ep1CalibrationOffsetKind, tuple[str, ...]] = {
    Ep1CalibrationOffsetKind.HUMIDITY: ("humidity_offset",),
    Ep1CalibrationOffsetKind.ILLUMINANCE: ("illuminance_offset", "illuminance_offset_ui"),
    Ep1CalibrationOffsetKind.TEMPERATURE: ("temperature_offset",),
}
_READING_ENTITY_ALIASES: dict[Ep1CalibrationOffsetKind, tuple[str, ...]] = {
    Ep1CalibrationOffsetKind.HUMIDITY: ("humidity", "humidity_sensor"),
    Ep1CalibrationOffsetKind.ILLUMINANCE: ("illuminance", "illuminance_sensor"),
    Ep1CalibrationOffsetKind.TEMPERATURE: ("temperature", "temperature_sensor"),
}
_STATE_COLLECT_TIMEOUT_S = 8.0


class Ep1CalibrationError(ValueError):
    """Operator-facing calibration failure (maps to HTTP 4xx/502)."""


class Ep1CalibrationNotFoundError(Ep1CalibrationError):
    """``device_id`` does not match a known EP1 target."""


class Ep1CalibrationValidationError(Ep1CalibrationError):
    """Offset out of range or required number entity missing."""


@dataclass(frozen=True, slots=True)
class Ep1CalibrationOffsetField:
    """One ESPHome offset number plus optional live sensor reading."""

    available: bool
    kind: Ep1CalibrationOffsetKind
    max_value: float | None
    min_value: float | None
    reading: float | None
    step: float | None
    unit: str | None
    value: float | None


@dataclass(frozen=True, slots=True)
class Ep1CalibrationSnapshot:
    """Calibration offsets + readings for one EP1 target."""

    device_id: str
    display_label: str
    display_name: str | None
    host: str
    offsets: Mapping[Ep1CalibrationOffsetKind, Ep1CalibrationOffsetField]
    port: int


@dataclass(frozen=True, slots=True)
class Ep1SettingsTarget:
    """One selectable EP1 for Settings Test / calibration."""

    device_id: str
    display_label: str
    display_name: str | None
    host: str
    port: int


async def apply_ep1_calibration_offsets(
    *,
    device_id: str,
    humidity_offset: float | None = None,
    illuminance_offset: float | None = None,
    temperature_offset: float | None = None,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1CalibrationSnapshot:
    """Write one or more offsets on ``device_id``, then return a fresh snapshot."""

    updates = _requested_offset_updates(
        humidity_offset=humidity_offset,
        illuminance_offset=illuminance_offset,
        temperature_offset=temperature_offset,
    )
    if not updates:
        return await read_ep1_calibration(
            device_id=device_id,
            cache_path=cache_path,
            cli_noise_psk=cli_noise_psk,
            ep1_mgr=ep1_mgr,
        )

    target = resolve_ep1_settings_target(
        device_id,
        cache_path=cache_path,
        ep1_mgr=ep1_mgr,
    )
    if target is None:
        raise Ep1CalibrationNotFoundError(EP1_CALIBRATION_DEVICE_NOT_FOUND.format(device_id=device_id))

    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        kind_to_number = _number_entities_by_offset_kind(entities)
        pending_writes: list[tuple[NumberInfo, float]] = []
        for kind, value in updates.items():
            number = kind_to_number.get(kind)
            if number is None:
                raise Ep1CalibrationValidationError(
                    EP1_CALIBRATION_OFFSET_UNAVAILABLE.format(
                        host=target.host,
                        port=target.port,
                        kind=kind.value,
                        aliases=_OFFSET_ENTITY_ALIASES[kind],
                    )
                )
            _validate_offset_in_range(kind=kind, number=number, value=value)
            pending_writes.append((number, float(value)))
        for number, value in pending_writes:
            # aioesphomeapi ``number_command`` is sync (sends NumberCommandRequest).
            client.number_command(int(number.key), value)
        # Brief settle so subscribe_states sees the new number + refreshed sensors.
        await asyncio.sleep(0.35)
        return await _snapshot_from_client(client, target=target, entities=entities)
    except APIConnectionError as exc:
        raise Ep1CalibrationError(f"EP1 calibration write failed at {target.host}:{target.port}: {exc}") from exc
    finally:
        await _disconnect_client(client)


def list_ep1_settings_targets(
    *,
    cache_path: Path | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> list[Ep1SettingsTarget]:
    """Return known EP1 targets (live manager first, then discovery cache)."""

    by_id: dict[str, Ep1SettingsTarget] = {}
    mgr = ep1_mgr if ep1_mgr is not None else _live_ep1_manager()
    if mgr is not None:
        try:
            devices = list(mgr.devices)
        except NotInitializedError:
            devices = []
        for device in devices:
            target = _target_from_live_device(device)
            if target is not None:
                by_id[target.device_id] = target

    path = cache_path if cache_path is not None else runtime.discovery_cache_path()
    if path is not None:
        for host, port, mac, friendly_name in device_discovery_store.load_ep1_devices(path):
            target = _target_from_cache_row(
                host=host,
                port=port,
                mac=mac,
                friendly_name=friendly_name,
            )
            if target is None:
                continue
            by_id.setdefault(target.device_id, target)

    return sorted(by_id.values(), key=lambda row: (row.display_label.casefold(), row.device_id))


async def read_ep1_calibration(
    *,
    device_id: str,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1CalibrationSnapshot:
    """Connect to ``device_id`` and read offset numbers + climate/light sensors."""

    target = resolve_ep1_settings_target(
        device_id,
        cache_path=cache_path,
        ep1_mgr=ep1_mgr,
    )
    if target is None:
        raise Ep1CalibrationNotFoundError(EP1_CALIBRATION_DEVICE_NOT_FOUND.format(device_id=device_id))

    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        return await _snapshot_from_client(client, target=target, entities=entities)
    except APIConnectionError as exc:
        raise Ep1CalibrationError(f"EP1 calibration read failed at {target.host}:{target.port}: {exc}") from exc
    finally:
        await _disconnect_client(client)


def resolve_ep1_settings_target(
    device_id: str,
    *,
    cache_path: Path | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1SettingsTarget | None:
    """Resolve ``device_id`` (MAC) to a host/port target."""

    needle = (device_id or "").strip().lower()
    if not needle:
        return None
    for target in list_ep1_settings_targets(cache_path=cache_path, ep1_mgr=ep1_mgr):
        if target.device_id == needle:
            return target
    return None


async def _collect_states_async(
    client: APIClient,
    keys: set[int],
    *,
    timeout_s: float = _STATE_COLLECT_TIMEOUT_S,
) -> dict[int, EntityState]:
    if not keys:
        return {}
    collected: dict[int, EntityState] = {}
    done = asyncio.Event()

    def _on_state(state: EntityState) -> None:
        if state.key not in keys:
            return
        collected[int(state.key)] = state
        if keys <= set(collected):
            done.set()

    client.subscribe_states(_on_state)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except TimeoutError:
        _LOGGER.debug(
            "EP1 calibration state collect timed out after %.1fs (got %s)",
            timeout_s,
            sorted(collected),
        )
    return collected


async def _disconnect_client(client: APIClient) -> None:
    try:
        await client.disconnect(force=True)
    except Exception:
        _LOGGER.debug("EP1 calibration client disconnect failed", exc_info=True)


def _ep1_api_client(*, host: str, port: int, noise_psk: str | None) -> APIClient:
    return APIClient(
        host,
        port,
        password=None,
        noise_psk=noise_psk,
        client_info="domesti-bot-ep1-calibration",
    )


def _float_from_number_state(state: EntityState | None) -> float | None:
    if not isinstance(state, NumberState):
        return None
    if getattr(state, "missing_state", False):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _float_from_sensor_state(state: EntityState | None) -> float | None:
    if not isinstance(state, SensorState):
        return None
    if getattr(state, "missing_state", False):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _live_ep1_manager() -> Ep1DeviceManager | None:
    state = runtime.device_state
    if state is None:
        return None
    return state.ep1_mgr


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _number_entities_by_offset_kind(
    entities: Sequence[EntityInfo],
) -> dict[Ep1CalibrationOffsetKind, NumberInfo]:
    out: dict[Ep1CalibrationOffsetKind, NumberInfo] = {}
    for entity in entities:
        if not isinstance(entity, NumberInfo):
            continue
        kind = _offset_kind_for_number(entity)
        if kind is None or kind in out:
            continue
        out[kind] = entity
    return out


def _offset_field_unavailable(kind: Ep1CalibrationOffsetKind) -> Ep1CalibrationOffsetField:
    return Ep1CalibrationOffsetField(
        available=False,
        kind=kind,
        max_value=None,
        min_value=None,
        reading=None,
        step=None,
        unit=None,
        value=None,
    )


def _offset_kind_for_number(entity: NumberInfo) -> Ep1CalibrationOffsetKind | None:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    for kind, aliases in _OFFSET_ENTITY_ALIASES.items():
        if tokens.intersection(aliases):
            return kind
    return None


def _reading_kind_for_sensor(entity: SensorInfo) -> Ep1CalibrationOffsetKind | None:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    for kind, aliases in _READING_ENTITY_ALIASES.items():
        if tokens.intersection(aliases):
            return kind
    return None


def _requested_offset_updates(
    *,
    humidity_offset: float | None,
    illuminance_offset: float | None,
    temperature_offset: float | None,
) -> dict[Ep1CalibrationOffsetKind, float]:
    updates: dict[Ep1CalibrationOffsetKind, float] = {}
    if humidity_offset is not None:
        updates[Ep1CalibrationOffsetKind.HUMIDITY] = float(humidity_offset)
    if illuminance_offset is not None:
        updates[Ep1CalibrationOffsetKind.ILLUMINANCE] = float(illuminance_offset)
    if temperature_offset is not None:
        updates[Ep1CalibrationOffsetKind.TEMPERATURE] = float(temperature_offset)
    return updates


def _resolved_noise_psk(*, cli_noise_psk: str | None, cache_path: Path | None) -> str | None:
    psk, _source = resolve_ep1_noise_psk(cli_psk=cli_noise_psk, cache_path=cache_path)
    return (psk or "").strip() or None


def _sensor_entities_by_reading_kind(
    entities: Sequence[EntityInfo],
) -> dict[Ep1CalibrationOffsetKind, SensorInfo]:
    out: dict[Ep1CalibrationOffsetKind, SensorInfo] = {}
    for entity in entities:
        if not isinstance(entity, SensorInfo):
            continue
        kind = _reading_kind_for_sensor(entity)
        if kind is None or kind in out:
            continue
        out[kind] = entity
    return out


async def _snapshot_from_client(
    client: APIClient,
    *,
    target: Ep1SettingsTarget,
    entities: Sequence[EntityInfo],
) -> Ep1CalibrationSnapshot:
    numbers = _number_entities_by_offset_kind(entities)
    sensors = _sensor_entities_by_reading_kind(entities)
    keys: set[int] = set()
    for number in numbers.values():
        keys.add(int(number.key))
    for sensor in sensors.values():
        keys.add(int(sensor.key))
    states = await _collect_states_async(client, keys)
    if keys and not states:
        raise Ep1CalibrationError(EP1_CALIBRATION_STATE_TIMEOUT.format(host=target.host, port=target.port))

    offsets: dict[Ep1CalibrationOffsetKind, Ep1CalibrationOffsetField] = {}
    for kind in Ep1CalibrationOffsetKind:
        number = numbers.get(kind)
        sensor = sensors.get(kind)
        reading = _float_from_sensor_state(states.get(int(sensor.key))) if sensor is not None else None
        if number is None:
            field = _offset_field_unavailable(kind)
            if reading is not None:
                field = Ep1CalibrationOffsetField(
                    available=False,
                    kind=kind,
                    max_value=None,
                    min_value=None,
                    reading=reading,
                    step=None,
                    unit=None,
                    value=None,
                )
            offsets[kind] = field
            continue
        offsets[kind] = Ep1CalibrationOffsetField(
            available=True,
            kind=kind,
            max_value=float(number.max_value),
            min_value=float(number.min_value),
            reading=reading,
            step=float(number.step),
            unit=(number.unit_of_measurement or "").strip() or None,
            value=_float_from_number_state(states.get(int(number.key))),
        )
    return Ep1CalibrationSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        offsets=offsets,
        port=target.port,
    )


def _target_from_cache_row(
    *,
    host: str,
    port: int,
    mac: str | None,
    friendly_name: str | None,
) -> Ep1SettingsTarget | None:
    device_id = try_normalize_mac(mac) if mac else None
    if device_id is None:
        return None
    display_name = (friendly_name or "").strip() or None
    return Ep1SettingsTarget(
        device_id=device_id,
        display_label=format_device_display(device_id, display_name),
        display_name=display_name,
        host=host.strip(),
        port=int(port) if port else DEFAULT_EP1_API_PORT,
    )


def _target_from_live_device(device: Ep1Device) -> Ep1SettingsTarget | None:
    mac = try_normalize_mac(device.mac_address or device.identifier)
    if mac is None:
        return None
    label = (device.preferred_label or "").strip() or None
    display_name = None if label is None or label.casefold() == mac.casefold() else label
    return Ep1SettingsTarget(
        device_id=mac,
        display_label=format_device_display(mac, display_name),
        display_name=display_name,
        host=device.host,
        port=int(device.port),
    )


def _validate_offset_in_range(
    *,
    kind: Ep1CalibrationOffsetKind,
    number: NumberInfo,
    value: float,
) -> None:
    lo = float(number.min_value)
    hi = float(number.max_value)
    if not math.isfinite(value):
        raise Ep1CalibrationValidationError(f"Expected a finite {kind.value} offset, got {value!r}")
    if value < lo or value > hi:
        raise Ep1CalibrationValidationError(f"Expected {kind.value} offset in [{lo}, {hi}], got {value}")
