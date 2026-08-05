"""EP1 ESPHome ``bluetooth_proxy`` select + BLE advertisement test.

Settings reads and writes the stock Everything Presence One ``bluetooth_proxy``
select entity (``Disabled`` / ``Enabled``) with an ephemeral
:class:`~aioesphomeapi.client.APIClient` so the live subscription watcher is not
disrupted. The Enable-and-Test path optionally enables the proxy, then samples
raw BLE advertisements for a short listen window.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aioesphomeapi.client import APIClient
from aioesphomeapi.core import APIConnectionError
from aioesphomeapi.model import EntityInfo, EntityState, SelectInfo, SelectState

from app.device_enums import Ep1BluetoothProxyState
from app.ep1_calibration import Ep1SettingsTarget, resolve_ep1_settings_target
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import Ep1DeviceManager

_LOGGER = logging.getLogger(__name__)

DEFAULT_BLE_LISTEN_DURATION_S = 20.0
DEFAULT_BLE_SAMPLE_LIMIT = 50
MAX_BLE_LISTEN_DURATION_S = 60.0
EP1_BLUETOOTH_PROXY_DEVICE_NOT_FOUND = "No EP1 device matched device_id={device_id!r}"
EP1_BLUETOOTH_PROXY_ENTITY_ALIASES: tuple[str, ...] = ("bluetooth_proxy",)
EP1_BLUETOOTH_PROXY_ENTITY_MISSING = "EP1 at {host}:{port} has no bluetooth_proxy select entity"
EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS: dict[str, str] = {
    "dd:36:02:01:09:74": "niece-chariot",
}
EP1_BLUETOOTH_PROXY_LISTEN_DISCONNECTED = "EP1 connection dropped during BLE listen at {host}:{port}"
EP1_BLUETOOTH_PROXY_NOT_ENABLED_FOR_PROBE = (
    "Bluetooth proxy must be Enabled when enable_if_needed is false (got {state!r})."
)
EP1_BLUETOOTH_PROXY_READ_FAILED = "EP1 bluetooth_proxy read failed at {host}:{port}: {exc}"
EP1_BLUETOOTH_PROXY_SELECT_COMMAND_FAILED = "EP1 bluetooth_proxy select_command failed at {host}:{port}: {exc}"
EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED = "EP1 bluetooth_proxy select did not confirm {expected!r} at {host}:{port}"
EP1_BLUETOOTH_PROXY_TEST_FAILED = "EP1 bluetooth_proxy test failed at {host}:{port}: {exc}"
EP1_BLUETOOTH_PROXY_TEST_OK = "Heard {count} BLE advertisement sample(s) in {duration_s:g}s."
EP1_BLUETOOTH_PROXY_TEST_ZERO_ADS = (
    "Bluetooth proxy is Enabled but no BLE advertisements were received in {duration_s:g}s."
)
EP1_BLUETOOTH_PROXY_WRITE_FAILED = "EP1 bluetooth_proxy write failed at {host}:{port}: {exc}"

_STATE_COLLECT_TIMEOUT_S = 3.0
_STATE_CONFIRM_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class Ep1BleAdvertisementSample:
    """One sampled BLE advertisement from the EP1 bluetooth proxy."""

    address: str
    address_type: int | str | None
    data_length: int | None
    known_test_beacon_label: str | None
    rssi: int | None


class Ep1BluetoothProxyError(ValueError):
    """Operator-facing bluetooth_proxy failure (maps to HTTP 4xx/502)."""


class Ep1BluetoothProxyNotFoundError(Ep1BluetoothProxyError):
    """``device_id`` does not match a known EP1 target."""


@dataclass(frozen=True, slots=True)
class Ep1BluetoothProxySnapshot:
    """``bluetooth_proxy`` select state for one EP1 Settings target."""

    available: bool
    device_id: str
    display_label: str
    display_name: str | None
    host: str
    options: tuple[str, ...]
    port: int
    state: Ep1BluetoothProxyState | None


@dataclass(frozen=True, slots=True)
class Ep1BluetoothProxyTestResult:
    """Outcome of an Enable-and-Test BLE listen on one EP1."""

    detail: str
    duration_s: float
    ok: bool
    proxy_state: Ep1BluetoothProxyState | None
    proxy_was_enabled: bool
    samples: tuple[Ep1BleAdvertisementSample, ...]


class Ep1BluetoothProxyValidationError(Ep1BluetoothProxyError):
    """Required select entity missing or request parameters invalid."""


async def probe_ep1_bluetooth_proxy(
    *,
    device_id: str,
    duration_s: float = DEFAULT_BLE_LISTEN_DURATION_S,
    sample_limit: int = DEFAULT_BLE_SAMPLE_LIMIT,
    enable_if_needed: bool = True,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1BluetoothProxyTestResult:
    """Enable ``bluetooth_proxy`` if needed, listen for BLE ads, return samples."""

    if not math.isfinite(duration_s) or not 0 < duration_s <= MAX_BLE_LISTEN_DURATION_S:
        raise Ep1BluetoothProxyValidationError(
            f"Expected duration_s in (0, {MAX_BLE_LISTEN_DURATION_S}], got {duration_s!r}"
        )
    if sample_limit <= 0:
        raise Ep1BluetoothProxyValidationError(f"Expected a positive sample_limit, got {sample_limit!r}")

    target = _require_target(device_id, cache_path=cache_path, ep1_mgr=ep1_mgr)
    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    disconnected = asyncio.Event()

    async def _on_stop(_expected_disconnect: bool) -> None:
        disconnected.set()

    try:
        await client.connect(on_stop=_on_stop, login=True)
        entities, _services = await client.list_entities_services()
        select = _require_bluetooth_proxy_select(entities, host=target.host, port=target.port)
        states = await _collect_states_async(client, {int(select.key)})
        current = _parse_proxy_state(states.get(int(select.key)))
        proxy_was_enabled = current == Ep1BluetoothProxyState.ENABLED
        if not enable_if_needed and current != Ep1BluetoothProxyState.ENABLED:
            raise Ep1BluetoothProxyValidationError(EP1_BLUETOOTH_PROXY_NOT_ENABLED_FOR_PROBE.format(state=current))
        if enable_if_needed and current != Ep1BluetoothProxyState.ENABLED:
            try:
                client.select_command(int(select.key), Ep1BluetoothProxyState.ENABLED.value)
            except Exception as exc:
                raise Ep1BluetoothProxyError(
                    EP1_BLUETOOTH_PROXY_SELECT_COMMAND_FAILED.format(host=target.host, port=target.port, exc=exc)
                ) from exc
            await _wait_for_select_state(
                client,
                key=int(select.key),
                expected=Ep1BluetoothProxyState.ENABLED.value,
                host=target.host,
                port=target.port,
            )
            current = Ep1BluetoothProxyState.ENABLED

        by_address: dict[str, Ep1BleAdvertisementSample] = {}

        def _on_advertisements(response: object) -> None:
            _ingest_advertisements(
                response,
                by_address=by_address,
                sample_limit=sample_limit,
            )

        unsubscribe = client.subscribe_bluetooth_le_raw_advertisements(_on_advertisements)
        try:
            await _wait_for_ble_listen(
                duration_s=duration_s,
                disconnected=disconnected,
                host=target.host,
                port=target.port,
            )
        finally:
            unsubscribe()

        samples = tuple(sorted(by_address.values(), key=lambda row: (row.address.casefold(), row.address)))
        if samples:
            detail = EP1_BLUETOOTH_PROXY_TEST_OK.format(count=len(samples), duration_s=duration_s)
        else:
            detail = EP1_BLUETOOTH_PROXY_TEST_ZERO_ADS.format(duration_s=duration_s)
        return Ep1BluetoothProxyTestResult(
            detail=detail,
            duration_s=float(duration_s),
            ok=True,
            proxy_state=current,
            proxy_was_enabled=proxy_was_enabled,
            samples=samples,
        )
    except APIConnectionError as exc:
        raise Ep1BluetoothProxyError(
            EP1_BLUETOOTH_PROXY_TEST_FAILED.format(host=target.host, port=target.port, exc=exc)
        ) from exc
    finally:
        await _disconnect_client(client)


async def read_ep1_bluetooth_proxy(
    *,
    device_id: str,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1BluetoothProxySnapshot:
    """Connect to ``device_id`` and read the ``bluetooth_proxy`` select state."""

    target = _require_target(device_id, cache_path=cache_path, ep1_mgr=ep1_mgr)
    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        return await _snapshot_from_client(client, target=target, entities=entities)
    except APIConnectionError as exc:
        raise Ep1BluetoothProxyError(
            EP1_BLUETOOTH_PROXY_READ_FAILED.format(host=target.host, port=target.port, exc=exc)
        ) from exc
    finally:
        await _disconnect_client(client)


async def set_ep1_bluetooth_proxy(
    *,
    device_id: str,
    enabled: bool,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1BluetoothProxySnapshot:
    """Set ``bluetooth_proxy`` to Enabled/Disabled, then return a fresh snapshot."""

    target = _require_target(device_id, cache_path=cache_path, ep1_mgr=ep1_mgr)
    option = Ep1BluetoothProxyState.ENABLED if enabled else Ep1BluetoothProxyState.DISABLED
    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        select = _require_bluetooth_proxy_select(entities, host=target.host, port=target.port)
        try:
            client.select_command(int(select.key), option.value)
        except Exception as exc:
            raise Ep1BluetoothProxyError(
                EP1_BLUETOOTH_PROXY_SELECT_COMMAND_FAILED.format(host=target.host, port=target.port, exc=exc)
            ) from exc
        await _wait_for_select_state(
            client,
            key=int(select.key),
            expected=option.value,
            host=target.host,
            port=target.port,
        )
        return await _snapshot_from_client(client, target=target, entities=entities)
    except APIConnectionError as exc:
        raise Ep1BluetoothProxyError(
            EP1_BLUETOOTH_PROXY_WRITE_FAILED.format(host=target.host, port=target.port, exc=exc)
        ) from exc
    finally:
        await _disconnect_client(client)


def _advertisements_from_response(response: object) -> Iterable[object]:
    advertisements = getattr(response, "advertisements", None)
    if isinstance(advertisements, Iterable) and not isinstance(advertisements, (bytes, str)):
        return advertisements
    if hasattr(response, "address"):
        return (response,)
    return ()


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
            "EP1 bluetooth_proxy state collect timed out after %.1fs (got %s)",
            timeout_s,
            sorted(collected),
        )
    return collected


async def _disconnect_client(client: APIClient) -> None:
    try:
        await client.disconnect(force=True)
    except Exception:
        _LOGGER.debug("EP1 bluetooth_proxy client disconnect failed", exc_info=True)


def _ep1_api_client(*, host: str, port: int, noise_psk: str | None) -> APIClient:
    return APIClient(
        host,
        port,
        password=None,
        noise_psk=noise_psk,
        client_info="domesti-bot-ep1-bluetooth-proxy",
    )


def _find_bluetooth_proxy_select(entities: Sequence[EntityInfo]) -> SelectInfo | None:
    for entity in entities:
        if not isinstance(entity, SelectInfo):
            continue
        tokens = {
            _normalize_entity_token(getattr(entity, "name", "") or ""),
            _normalize_entity_token(getattr(entity, "object_id", "") or ""),
        }
        tokens.discard("")
        if tokens.intersection(EP1_BLUETOOTH_PROXY_ENTITY_ALIASES):
            return entity
    return None


def _format_ble_address(address: object) -> str:
    if isinstance(address, int):
        if 0 <= address < 1 << 48:
            return address.to_bytes(6, "big", signed=False).hex(":")
        return str(address)
    return str(address)


def _ingest_advertisements(
    response: object,
    *,
    by_address: dict[str, Ep1BleAdvertisementSample],
    sample_limit: int,
) -> None:
    for advertisement in _advertisements_from_response(response):
        address = _format_ble_address(getattr(advertisement, "address", "unknown"))
        rssi_raw = getattr(advertisement, "rssi", None)
        rssi: int | None
        try:
            rssi = int(rssi_raw) if rssi_raw is not None else None
        except (TypeError, ValueError):
            rssi = None
        address_type_raw = getattr(advertisement, "address_type", None)
        address_type: int | str | None
        if address_type_raw is None:
            address_type = None
        elif isinstance(address_type_raw, (int, str)):
            address_type = address_type_raw
        else:
            address_type = str(address_type_raw)
        data = getattr(advertisement, "data", None)
        data_length = len(data) if isinstance(data, bytes | bytearray) else None
        sample = Ep1BleAdvertisementSample(
            address=address,
            address_type=address_type,
            data_length=data_length,
            known_test_beacon_label=EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS.get(address.casefold()),
            rssi=rssi,
        )
        existing = by_address.get(address)
        if existing is None:
            if len(by_address) >= sample_limit:
                continue
            by_address[address] = sample
            continue
        if _rssi_is_stronger(candidate=rssi, incumbent=existing.rssi):
            by_address[address] = sample


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _parse_proxy_state(state: EntityState | None) -> Ep1BluetoothProxyState | None:
    if not isinstance(state, SelectState):
        return None
    if getattr(state, "missing_state", False):
        return None
    raw = (state.state or "").strip()
    if not raw:
        return None
    try:
        return Ep1BluetoothProxyState(raw)
    except ValueError:
        return None


def _require_bluetooth_proxy_select(
    entities: Sequence[EntityInfo],
    *,
    host: str,
    port: int,
) -> SelectInfo:
    select = _find_bluetooth_proxy_select(entities)
    if select is None:
        raise Ep1BluetoothProxyValidationError(EP1_BLUETOOTH_PROXY_ENTITY_MISSING.format(host=host, port=port))
    return select


def _require_target(
    device_id: str,
    *,
    cache_path: Path | None,
    ep1_mgr: Ep1DeviceManager | None,
) -> Ep1SettingsTarget:
    target = resolve_ep1_settings_target(
        device_id,
        cache_path=cache_path,
        ep1_mgr=ep1_mgr,
    )
    if target is None:
        raise Ep1BluetoothProxyNotFoundError(EP1_BLUETOOTH_PROXY_DEVICE_NOT_FOUND.format(device_id=device_id))
    return target


def _resolved_noise_psk(*, cli_noise_psk: str | None, cache_path: Path | None) -> str | None:
    psk, _source = resolve_ep1_noise_psk(cli_psk=cli_noise_psk, cache_path=cache_path)
    return (psk or "").strip() or None


def _rssi_is_stronger(*, candidate: int | None, incumbent: int | None) -> bool:
    if candidate is None:
        return False
    if incumbent is None:
        return True
    return candidate > incumbent


async def _snapshot_from_client(
    client: APIClient,
    *,
    target: Ep1SettingsTarget,
    entities: Sequence[EntityInfo],
) -> Ep1BluetoothProxySnapshot:
    select = _find_bluetooth_proxy_select(entities)
    if select is None:
        return Ep1BluetoothProxySnapshot(
            available=False,
            device_id=target.device_id,
            display_label=target.display_label,
            display_name=target.display_name,
            host=target.host,
            options=(),
            port=target.port,
            state=None,
        )
    options = tuple(str(option) for option in (select.options or ()))
    states = await _collect_states_async(client, {int(select.key)})
    return Ep1BluetoothProxySnapshot(
        available=True,
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        options=options,
        port=target.port,
        state=_parse_proxy_state(states.get(int(select.key))),
    )


async def _wait_for_ble_listen(
    *,
    duration_s: float,
    disconnected: asyncio.Event,
    host: str,
    port: int,
) -> None:
    """Sleep for ``duration_s`` unless the ESPHome session drops first."""

    sleep_task = asyncio.create_task(asyncio.sleep(duration_s))
    disc_task = asyncio.create_task(disconnected.wait())
    try:
        done, _pending = await asyncio.wait(
            {sleep_task, disc_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if disc_task in done:
            raise Ep1BluetoothProxyError(EP1_BLUETOOTH_PROXY_LISTEN_DISCONNECTED.format(host=host, port=port))
    finally:
        for task in (sleep_task, disc_task):
            task.cancel()
        for task in (sleep_task, disc_task):
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _wait_for_select_state(
    client: APIClient,
    *,
    key: int,
    expected: str,
    host: str,
    port: int,
    timeout_s: float = _STATE_CONFIRM_TIMEOUT_S,
) -> None:
    done = asyncio.Event()

    def _on_state(state: EntityState) -> None:
        if int(state.key) != key:
            return
        if not isinstance(state, SelectState):
            return
        if getattr(state, "missing_state", False):
            return
        if (state.state or "").strip() == expected:
            done.set()

    client.subscribe_states(_on_state)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except TimeoutError as exc:
        _LOGGER.warning(
            "EP1 bluetooth_proxy select confirm timed out at %s:%s (expected %r)",
            host,
            port,
            expected,
        )
        raise Ep1BluetoothProxyError(
            EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED.format(expected=expected, host=host, port=port)
        ) from exc
