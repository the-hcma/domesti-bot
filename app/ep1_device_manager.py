"""Everything Presence One (EP1) device manager via ESPHome native API.

Cache-first host reconnect; occupancy + climate/light readings from a short
``subscribe_states`` dump during :meth:`Ep1DeviceManager.fetch`. Long-lived
subscriptions belong in the event watcher follow-on (#522).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from aioesphomeapi.client import APIClient
from aioesphomeapi.model import (
    BinarySensorInfo,
    BinarySensorState,
    EntityInfo,
    EntityState,
    SensorInfo,
    SensorState,
)

from app import device_discovery_store
from app.device_enums import DeviceConditionState
from app.device_mac import try_normalize_mac
from app.device_manager import AlreadyInitializedError, DeviceManager, NotInitializedError
from app.ep1_credentials import resolve_ep1_noise_psk
from app.rule_engine import Device

_LOGGER = logging.getLogger(__name__)

DEFAULT_EP1_API_PORT = 6053
_ENTITY_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "humidity": ("humidity", "humidity_sensor"),
    "illuminance": ("illuminance", "illuminance_sensor"),
    "occupancy": ("occupancy",),
    "temperature": ("temperature", "temperature_sensor"),
}
_STATE_COLLECT_TIMEOUT_S = 8.0


class Ep1Device(Device):
    """Read-only room occupancy sensor with cached climate / light readings."""

    __slots__ = (
        "_host",
        "_humidity_pct",
        "_illuminance_lx",
        "_mac_address",
        "_occupancy_bool",
        "_port",
        "_readings_updated_at",
        "_temperature_c",
    )

    def __init__(
        self,
        identifier: str,
        *,
        display_name: str | None = None,
        host: str,
        port: int = DEFAULT_EP1_API_PORT,
        mac_address: str | None = None,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._host = host.strip()
        self._port = int(port)
        self._mac_address = mac_address
        self._occupancy_bool: bool | None = None
        self._temperature_c: float | None = None
        self._humidity_pct: float | None = None
        self._illuminance_lx: float | None = None
        self._readings_updated_at: float | None = None

    @property
    def host(self) -> str:
        return self._host

    @property
    def humidity_pct(self) -> float | None:
        return self._humidity_pct

    @property
    def illuminance_lx(self) -> float | None:
        return self._illuminance_lx

    @property
    def mac_address(self) -> str | None:
        return self._mac_address

    @property
    def occupancy_state(self) -> str:
        """Wire value: ``occupied``, ``clear``, or ``unknown``."""
        if self._occupancy_bool is True:
            return DeviceConditionState.OCCUPIED.value
        if self._occupancy_bool is False:
            return DeviceConditionState.CLEAR.value
        return "unknown"

    @property
    def port(self) -> int:
        return self._port

    @property
    def readings_updated_at(self) -> float | None:
        return self._readings_updated_at

    @property
    def temperature_c(self) -> float | None:
        return self._temperature_c

    def apply_entity_state(
        self,
        *,
        occupancy: bool | None = None,
        temperature_c: float | None = None,
        humidity_pct: float | None = None,
        illuminance_lx: float | None = None,
        updated_at: float | None = None,
    ) -> None:
        """Merge one or more reading fields into the in-memory cache."""
        if occupancy is not None:
            self._occupancy_bool = occupancy
        if temperature_c is not None:
            self._temperature_c = temperature_c
        if humidity_pct is not None:
            self._humidity_pct = humidity_pct
        if illuminance_lx is not None:
            self._illuminance_lx = illuminance_lx
        self._readings_updated_at = updated_at if updated_at is not None else time.time()

    def set_endpoint(self, *, host: str, port: int) -> None:
        self._host = host.strip()
        self._port = int(port)

    def set_mac_address(self, mac: str | None) -> None:
        self._mac_address = mac


class Ep1DeviceManager(DeviceManager[Ep1Device]):
    """Discover / reconnect EP1 sensors and cache occupancy + climate readings."""

    def __init__(
        self,
        *,
        configured_hosts: Sequence[tuple[str, int]] | None = None,
        discovery_cache_path: Path | None = None,
        cli_noise_psk: str | None = None,
        noise_psk: str | None = None,
        force_discovery: bool = False,
        state_collect_timeout_s: float = _STATE_COLLECT_TIMEOUT_S,
        api_client_factory: Callable[..., APIClient] | None = None,
    ) -> None:
        self._configured_hosts = [(h.strip(), int(p)) for h, p in (configured_hosts or ()) if h.strip()]
        self._discovery_cache_path = discovery_cache_path
        self._cli_noise_psk = cli_noise_psk
        self._noise_psk = (noise_psk or "").strip() or None
        self._force_discovery = force_discovery
        self._state_collect_timeout_s = float(state_collect_timeout_s)
        self._api_client_factory = api_client_factory or APIClient
        self._devices: dict[str, Ep1Device] = {}
        self._clients: list[APIClient] = []
        self._fetched = False
        self._last_discovery_source: str | None = None

    @property
    def devices(self) -> list[Ep1Device]:
        if not self._fetched:
            raise NotInitializedError("Ep1DeviceManager.fetch() has not completed")
        return sorted(self._devices.values(), key=lambda d: (d.preferred_label.lower(), d.identifier))

    @property
    def last_discovery_source(self) -> str | None:
        return self._last_discovery_source

    @property
    def sensors(self) -> list[Ep1Device]:
        """Alias for :attr:`devices` (occupancy sensors)."""
        return self.devices

    async def disconnect(self) -> None:
        clients = list(self._clients)
        self._clients.clear()
        for client in clients:
            try:
                await client.disconnect(force=True)
            except Exception:
                _LOGGER.debug("EP1 client disconnect failed", exc_info=True)

    async def fetch(self) -> None:
        if self._fetched:
            raise AlreadyInitializedError("Ep1DeviceManager.fetch() already completed")
        if self._noise_psk is not None:
            psk = self._noise_psk
        else:
            psk, _source = resolve_ep1_noise_psk(
                cli_psk=self._cli_noise_psk,
                cache_path=self._discovery_cache_path,
            )
        targets = self._initial_targets()
        if not targets:
            self._fetched = True
            self._last_discovery_source = None
            return
        if not psk:
            _LOGGER.warning(
                "EP1 hosts configured but no Noise PSK — set EP1_NOISE_PSK, --ep1-noise-psk, or Settings → EP1"
            )
            self._fetched = True
            self._last_discovery_source = None
            return

        used_cache_only = bool(self._cache_targets()) and not self._force_discovery
        connected_any = False
        for host, port in targets:
            try:
                device = await self._connect_and_read(host=host, port=port, noise_psk=psk)
            except Exception as exc:
                _LOGGER.warning("EP1 connect failed for %s:%s: %s", host, port, exc)
                continue
            if device is None:
                continue
            connected_any = True
            self._devices[device.identifier] = device
            if self._discovery_cache_path is not None:
                device_discovery_store.upsert_ep1_device(
                    self._discovery_cache_path,
                    host=device.host,
                    port=device.port,
                    mac=device.mac_address,
                    friendly_name=device.display_name,
                )

        self._fetched = True
        if connected_any:
            self._last_discovery_source = "cache" if used_cache_only else "discovery"
        else:
            self._last_discovery_source = None

    async def refresh_device_readings(self, identifier: str) -> None:
        """Re-read one device (for future watcher / Settings test paths)."""
        device = self._devices.get(identifier)
        if device is None:
            raise KeyError(identifier)
        if self._noise_psk is not None:
            psk = self._noise_psk
        else:
            psk, _source = resolve_ep1_noise_psk(
                cli_psk=self._cli_noise_psk,
                cache_path=self._discovery_cache_path,
            )
        if not psk:
            raise RuntimeError("Expected a Noise PSK for EP1 refresh, got none")
        updated = await self._connect_and_read(
            host=device.host,
            port=device.port,
            noise_psk=psk,
        )
        if updated is None:
            raise RuntimeError(f"EP1 refresh failed for {identifier}")
        self._devices[identifier] = updated

    def _cache_targets(self) -> list[tuple[str, int]]:
        if self._discovery_cache_path is None:
            return []
        rows = device_discovery_store.load_ep1_devices(self._discovery_cache_path)
        return [(host, port) for host, port, _mac, _name in rows]

    async def _collect_states(
        self,
        client: APIClient,
        *,
        key_to_role: dict[int, str],
    ) -> dict[str, EntityState]:
        if not key_to_role:
            return {}
        collected: dict[str, EntityState] = {}
        done = asyncio.Event()

        def _on_state(state: EntityState) -> None:
            role = key_to_role.get(state.key)
            if role is None:
                return
            collected[role] = state
            if set(key_to_role.values()) <= set(collected):
                done.set()

        client.subscribe_states(_on_state)
        try:
            await asyncio.wait_for(done.wait(), timeout=self._state_collect_timeout_s)
        except TimeoutError:
            _LOGGER.debug(
                "EP1 state collect timed out after %.1fs (got %s)",
                self._state_collect_timeout_s,
                sorted(collected),
            )
        return collected

    async def _connect_and_read(
        self,
        *,
        host: str,
        port: int,
        noise_psk: str,
    ) -> Ep1Device | None:
        client = self._api_client_factory(
            host,
            port,
            password=None,
            noise_psk=noise_psk,
            client_info="domesti-bot",
        )
        self._clients.append(client)
        keep_client = False
        try:
            await client.connect(login=True)
            info = await client.device_info()
            mac_raw = (info.mac_address or "").strip()
            mac = try_normalize_mac(mac_raw) if mac_raw else None
            if mac is None:
                _LOGGER.warning("Skipping EP1 at %s:%s — no usable MAC on device_info", host, port)
                return None

            friendly = (info.friendly_name or info.name or "").strip() or None
            entities, _services = await client.list_entities_services()
            key_to_role = _entity_key_to_role(entities)
            states = await self._collect_states(client, key_to_role=key_to_role)

            device = Ep1Device(
                mac,
                display_name=friendly,
                host=host,
                port=port,
                mac_address=mac,
            )
            occupancy = _occupancy_from_state(states.get("occupancy"))
            device.apply_entity_state(
                occupancy=occupancy,
                temperature_c=_float_from_sensor_state(states.get("temperature")),
                humidity_pct=_float_from_sensor_state(states.get("humidity")),
                illuminance_lx=_float_from_sensor_state(states.get("illuminance")),
            )
            keep_client = True
            return device
        finally:
            if not keep_client:
                await self._discard_client(client)

    async def _discard_client(self, client: APIClient) -> None:
        if client in self._clients:
            self._clients.remove(client)
        try:
            await client.disconnect(force=True)
        except Exception:
            _LOGGER.debug("EP1 client disconnect after failure failed", exc_info=True)

    def _initial_targets(self) -> list[tuple[str, int]]:
        """Return hosts to probe.

        With ``force_discovery``, only configured CLI/env hosts are used (cache
        ignored). Otherwise prefer the discovery cache, then configured hosts.
        """

        if self._force_discovery:
            return list(self._configured_hosts)
        cached = self._cache_targets()
        if cached:
            return cached
        return list(self._configured_hosts)


def _entity_key_to_role(entities: Sequence[EntityInfo]) -> dict[int, str]:
    key_to_role: dict[int, str] = {}
    for entity in entities:
        role = _role_for_entity(entity)
        if role is None:
            continue
        try:
            key_to_role[int(entity.key)] = role
        except (TypeError, ValueError):
            continue
    return key_to_role


def _float_from_sensor_state(state: EntityState | None) -> float | None:
    if not isinstance(state, SensorState):
        return None
    if getattr(state, "missing_state", False):
        return None
    value = state.state
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _occupancy_from_state(state: EntityState | None) -> bool | None:
    if not isinstance(state, BinarySensorState):
        return None
    if getattr(state, "missing_state", False):
        return None
    return bool(state.state)


def _role_for_entity(entity: EntityInfo) -> str | None:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    for role, aliases in _ENTITY_NAME_ALIASES.items():
        if not tokens.intersection(aliases):
            continue
        if role == "occupancy" and not isinstance(entity, BinarySensorInfo):
            continue
        if role != "occupancy" and not isinstance(entity, SensorInfo):
            continue
        return role
    return None
