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
from dataclasses import dataclass, replace
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

_NUMBER_VALUE_ABS_TOL = 1e-6
_OFFSET_ENTITY_ALIASES: dict[Ep1CalibrationOffsetKind, tuple[str, ...]] = {
    Ep1CalibrationOffsetKind.HUMIDITY: ("humidity_offset",),
    Ep1CalibrationOffsetKind.ILLUMINANCE: ("illuminance_offset", "illuminance_offset_ui"),
    Ep1CalibrationOffsetKind.TEMPERATURE: ("temperature_offset",),
}
_POST_WRITE_TIMEOUT_S = 8.0
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
    offsets_confirmed: bool = True
    readings_refreshed: bool = True


@dataclass(frozen=True, slots=True)
class Ep1CalibrationWriteSettle:
    """Post-write wait outcome for offset numbers vs linked sensor refresh."""

    offsets_confirmed: bool
    readings_refreshed: bool


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
        sensors_by_kind = _sensor_entities_by_reading_kind(entities)
        sensor_key_by_kind = {kind: int(sensors_by_kind[kind].key) for kind in updates if kind in sensors_by_kind}
        number_keys = {int(number.key) for number, _value in pending_writes}
        # Capture number + sensor states *before* the write so we can skip
        # sensor-refresh wait for offsets that are already at the requested value.
        baseline_keys = set(sensor_key_by_kind.values()) | number_keys
        baseline_states = await _collect_states_async(client, baseline_keys) if baseline_keys else {}
        unchanged_kinds = {
            kind
            for kind, number, value in (
                (k, kind_to_number[k], float(updates[k])) for k in updates if k in kind_to_number
            )
            if _number_state_matches_expected(baseline_states.get(int(number.key)), value)
        }
        sensor_keys = {key for kind, key in sensor_key_by_kind.items() if kind not in unchanged_kinds}
        sensor_baselines = {key: _float_from_sensor_state(baseline_states.get(key)) for key in sensor_keys}
        for number, value in pending_writes:
            # aioesphomeapi ``number_command`` is sync (sends NumberCommandRequest).
            client.number_command(int(number.key), value)
        settle = await _wait_for_calibration_write_effects(
            client,
            expected_numbers={int(number.key): value for number, value in pending_writes},
            sensor_keys=sensor_keys,
            sensor_baselines=sensor_baselines,
            host=target.host,
            port=target.port,
        )
        snapshot = await _snapshot_from_client(client, target=target, entities=entities)
        if settle.offsets_confirmed and settle.readings_refreshed:
            return snapshot
        return replace(
            snapshot,
            offsets_confirmed=settle.offsets_confirmed,
            readings_refreshed=settle.readings_refreshed,
        )
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


def _number_state_matches_expected(state: EntityState | None, expected: float) -> bool:
    actual = _float_from_number_state(state)
    if actual is None:
        return False
    return abs(actual - expected) <= max(_NUMBER_VALUE_ABS_TOL, abs(expected) * 1e-9)


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


def _sensor_reading_changed(
    *,
    baseline: float | None,
    current: float | None,
) -> bool:
    """True only when both sides are known and differ (no baseline ⇒ not a refresh)."""

    if current is None or baseline is None:
        return False
    return abs(current - baseline) > _NUMBER_VALUE_ABS_TOL


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


async def _wait_for_calibration_write_effects(
    client: APIClient,
    *,
    expected_numbers: Mapping[int, float],
    sensor_keys: set[int],
    sensor_baselines: Mapping[int, float | None] | None = None,
    host: str = "",
    port: int = 0,
    timeout_s: float = _POST_WRITE_TIMEOUT_S,
) -> Ep1CalibrationWriteSettle:
    """Wait until written offset numbers stick and linked sensors refresh.

    Stock EP1 firmware applies offsets via sensor filters and only republishes
    illuminance/temperature/humidity after the next sensor update (often >1s).
    Returning a snapshot too early shows the new offset beside a stale live
    reading, which looks like Apply did nothing.

    ``sensor_baselines`` must be captured *before* ``number_command``. After
    offset numbers confirm, only *subsequent* sensor state callbacks may
    satisfy the refresh wait (pre-write baselines are kept — never re-locked
    from the subscribe dump). Number confirm and sensor refresh are reported
    separately; soft-timeouts prefer a snapshot over raising after the write.
    """

    if not expected_numbers:
        return Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=True)

    baselines = dict(sensor_baselines or {})
    collected: dict[int, EntityState] = {}
    confirmed_numbers: set[int] = set()
    refreshed_sensors: set[int] = set()
    required_sensors: set[int] = set(sensor_keys)
    numbers_ready = asyncio.Event()
    sensors_ready = asyncio.Event()
    accept_sensor_refresh = False
    watch_keys = set(expected_numbers) | set(sensor_keys)
    sensors_unverified = False

    def _arm_sensor_refresh_wait() -> None:
        nonlocal accept_sensor_refresh, sensors_unverified
        # Keep pre-write baselines. Re-locking from ``collected`` can freeze an
        # already-refreshed reading as the baseline (multi-offset / early dump)
        # or treat a pre-confirm natural republish as success. Only *new*
        # post-confirm sensor callbacks may satisfy the wait.
        required_sensors.clear()
        required_sensors.update(key for key in sensor_keys if baselines.get(key) is not None)
        if sensor_keys and not required_sensors:
            # Linked sensors existed but none had a known baseline — unverified.
            sensors_unverified = True
            sensors_ready.set()
            accept_sensor_refresh = False
            return
        if not required_sensors:
            sensors_ready.set()
            accept_sensor_refresh = False
            return
        accept_sensor_refresh = True

    def _on_state(state: EntityState) -> None:
        key = int(state.key)
        if key not in watch_keys:
            return
        collected[key] = state
        if key in expected_numbers and _number_state_matches_expected(state, expected_numbers[key]):
            confirmed_numbers.add(key)
            if confirmed_numbers >= set(expected_numbers) and not numbers_ready.is_set():
                numbers_ready.set()
                if sensor_keys:
                    _arm_sensor_refresh_wait()
        if (
            accept_sensor_refresh
            and key in required_sensors
            and _sensor_reading_changed(
                baseline=baselines.get(key),
                current=_float_from_sensor_state(state),
            )
        ):
            refreshed_sensors.add(key)
            if refreshed_sensors >= required_sensors:
                sensors_ready.set()

    client.subscribe_states(_on_state)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    try:
        await asyncio.wait_for(numbers_ready.wait(), timeout=timeout_s)
    except TimeoutError:
        # number_command already ran; prefer returning a snapshot over 502 so
        # the UI can show whatever stuck rather than a hard failure.
        _LOGGER.info(
            "EP1 calibration post-write: offset numbers not confirmed after %.1fs "
            "(confirmed=%s expected=%s host=%s port=%s)",
            timeout_s,
            sorted(confirmed_numbers),
            sorted(expected_numbers),
            host or "?",
            port or "?",
        )
        return Ep1CalibrationWriteSettle(offsets_confirmed=False, readings_refreshed=False)

    if not sensor_keys:
        return Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=True)

    if sensors_ready.is_set() and not required_sensors:
        return Ep1CalibrationWriteSettle(
            offsets_confirmed=True,
            readings_refreshed=not sensors_unverified,
        )

    remaining = deadline - loop.time()
    if remaining <= 0:
        _LOGGER.info(
            "EP1 calibration post-write: no time left to wait for sensor refresh (sensors=%s)",
            sorted(required_sensors),
        )
        return Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=False)
    try:
        await asyncio.wait_for(sensors_ready.wait(), timeout=remaining)
    except TimeoutError:
        _LOGGER.info(
            "EP1 calibration post-write: sensor refresh timed out after %.1fs (refreshed=%s expected=%s)",
            remaining,
            sorted(refreshed_sensors),
            sorted(required_sensors),
        )
        return Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=False)
    return Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=True)
