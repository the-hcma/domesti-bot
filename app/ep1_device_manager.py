"""Everything Presence One (EP1) device manager via ESPHome native API.

Cache-first host reconnect; occupancy + climate/light readings from a short
``subscribe_states`` dump during :meth:`Ep1DeviceManager.fetch`. Long-lived
subscriptions are owned by :class:`~app.device_state_watcher.Ep1SubscriptionWatcher`
via :meth:`Ep1DeviceManager.run_subscription_session`.

Homey / pre-adoption stock firmware speaks the ESPHome native API in
**plaintext** (no Noise PSK). Encrypted firmware is optional: pass a PSK when
the device has ``api.encryption`` enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Sequence
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
from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from app import device_discovery_store
from app.device_enums import DeviceConditionState
from app.device_mac import try_normalize_mac
from app.device_manager import AlreadyInitializedError, DeviceManager, NotInitializedError
from app.ep1_credentials import resolve_ep1_noise_psk
from app.rule_engine import Device

_LOGGER = logging.getLogger(__name__)

DEFAULT_EP1_API_PORT = 6053
DEFAULT_EP1_ZEROCONF_TIMEOUT_S = 8.0

# ESPHome native API mDNS type (Homey / HA / stock EP1 installer).
_EP1_MDNS_TYPE = "_esphomelib._tcp.local."
_EP1_NAME_MARKERS = (
    "everything presence one",
    "everything-presence-one",
    "everything smart technology.everything presence one",
)
_ENTITY_NAME_ALIASES: dict[str, tuple[str, ...]] = {
    "humidity": ("humidity", "humidity_sensor"),
    "illuminance": ("illuminance", "illuminance_sensor"),
    "occupancy": ("occupancy",),
    "temperature": ("temperature", "temperature_sensor"),
}
_STATE_COLLECT_TIMEOUT_S = 8.0


class Ep1DiscoveryError(RuntimeError):
    """No Everything Presence One responded on the LAN within the discovery window."""


async def discover_ep1_hosts(*, timeout: float = DEFAULT_EP1_ZEROCONF_TIMEOUT_S) -> list[tuple[str, int]]:
    """Browse ``_esphomelib._tcp`` for Everything Presence One nodes.

    Returns ``(host, port)`` pairs (IPv4 preferred). Raises
    :class:`Ep1DiscoveryError` when none are found before ``timeout``.
    """
    found: dict[tuple[str, int], None] = {}
    done = asyncio.Event()
    zc = AsyncZeroconf()
    pending: set[asyncio.Task[None]] = set()

    async def resolve_service(service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zc.zeroconf, 3000)
        if not _is_ep1_service(info):
            return
        addr = _pick_ep1_host_address(info)
        if addr is None:
            return
        port = int(info.port) if info.port else DEFAULT_EP1_API_PORT
        found[(addr, port)] = None
        done.set()

    def on_service_state_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        if state_change not in (
            ServiceStateChange.Added,
            ServiceStateChange.Updated,
        ):
            return
        task = asyncio.create_task(resolve_service(service_type, name))
        pending.add(task)
        task.add_done_callback(pending.discard)

    browser = AsyncServiceBrowser(
        zc.zeroconf,
        _EP1_MDNS_TYPE,
        handlers=[on_service_state_change],
    )
    try:
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Keep browsing briefly after the first hit so siblings can appear.
            wait_s = min(remaining, 0.75 if found else remaining)
            try:
                await asyncio.wait_for(done.wait(), timeout=wait_s)
            except TimeoutError:
                if found:
                    break
                continue
            done.clear()
            if found and time.monotonic() + 0.5 >= deadline:
                break
        if not found:
            raise Ep1DiscoveryError(
                f"No Everything Presence One found on the LAN within {timeout}s "
                "(set --ep1-host / EP1_HOSTS or run on the same subnet)."
            )
        return sorted(found)
    finally:
        await browser.async_cancel()
        await zc.async_close()
        for task in list(pending):
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


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
        zeroconf_discovery: bool = True,
        zeroconf_timeout: float = DEFAULT_EP1_ZEROCONF_TIMEOUT_S,
        zeroconf_discover_fn: Callable[..., Awaitable[list[tuple[str, int]]]] | None = None,
    ) -> None:
        self._configured_hosts = [(h.strip(), int(p)) for h, p in (configured_hosts or ()) if h.strip()]
        self._discovery_cache_path = discovery_cache_path
        self._cli_noise_psk = cli_noise_psk
        self._noise_psk = (noise_psk or "").strip() or None
        self._force_discovery = force_discovery
        self._state_collect_timeout_s = float(state_collect_timeout_s)
        self._api_client_factory = api_client_factory or APIClient
        self._zeroconf_discovery = bool(zeroconf_discovery)
        self._zeroconf_timeout = float(zeroconf_timeout)
        self._zeroconf_discover_fn = zeroconf_discover_fn or discover_ep1_hosts
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

    @property
    def zeroconf_timeout(self) -> float:
        return self._zeroconf_timeout

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
        psk = self._resolved_noise_psk()
        targets, used_cache_only = await self._resolve_targets()
        if not targets:
            self._fetched = True
            self._last_discovery_source = None
            return
        if psk is None:
            _LOGGER.info("EP1 connecting without Noise PSK (plaintext Homey / pre-adoption firmware)")

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

    async def rediscover(
        self,
        *,
        hosts: Sequence[tuple[str, int]] | None = None,
    ) -> None:
        """Force a fresh probe (optional ``hosts`` override); fall back to cache if LAN finds nothing."""
        await self.disconnect()
        self._devices.clear()
        self._fetched = False
        previous = self._force_discovery
        previous_hosts = self._configured_hosts
        self._force_discovery = True
        if hosts is not None:
            self._configured_hosts = [(h.strip(), int(p)) for h, p in hosts if str(h).strip()]
        try:
            await self.fetch()
        finally:
            self._force_discovery = previous
            if hosts is not None:
                self._configured_hosts = previous_hosts

    async def refresh_device_readings(self, identifier: str) -> None:
        """Re-read one device (for Settings test / one-shot paths)."""
        device = self._devices.get(identifier)
        if device is None:
            raise KeyError(identifier)
        psk = self._resolved_noise_psk()
        updated = await self._connect_and_read(
            host=device.host,
            port=device.port,
            noise_psk=psk,
        )
        if updated is None:
            raise RuntimeError(f"EP1 refresh failed for {identifier}")
        self._devices[identifier] = updated

    async def run_subscription_session(
        self,
        device: Ep1Device,
        *,
        stop: asyncio.Event,
        on_reading_updated: Callable[[Ep1Device], None] | None = None,
    ) -> None:
        """Connect, ``subscribe_states``, and wait until ``stop`` or disconnect.

        One session only — callers (the subscription watcher) own reconnect
        backoff between sessions. Never blocks the event loop on LAN I/O
        beyond awaited aioesphomeapi coroutines. ``noise_psk`` may be empty for
        plaintext Homey firmware.
        """

        psk = self._resolved_noise_psk()

        disconnected = asyncio.Event()

        async def _on_stop(_expected_disconnect: bool) -> None:
            disconnected.set()

        client = self._api_client_factory(
            device.host,
            device.port,
            password=None,
            noise_psk=psk,
            client_info="domesti-bot",
        )
        self._clients.append(client)
        try:
            await client.connect(on_stop=_on_stop, login=True)
            entities, _services = await client.list_entities_services()
            key_to_role = _entity_key_to_role(entities)

            def _on_state(state: EntityState) -> None:
                _apply_entity_state_to_device(
                    device,
                    state,
                    key_to_role=key_to_role,
                )
                if on_reading_updated is not None:
                    on_reading_updated(device)

            client.subscribe_states(_on_state)
            stop_task = asyncio.create_task(stop.wait())
            disc_task = asyncio.create_task(disconnected.wait())
            try:
                done, _pending = await asyncio.wait(
                    {stop_task, disc_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                del done
            finally:
                for task in (stop_task, disc_task):
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
        finally:
            await self._discard_client(client)

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
        noise_psk: str | None,
    ) -> Ep1Device | None:
        """One-shot connect + state dump; always disconnects before returning."""

        client = self._api_client_factory(
            host,
            port,
            password=None,
            noise_psk=noise_psk,
            client_info="domesti-bot",
        )
        self._clients.append(client)
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
            return device
        finally:
            await self._discard_client(client)

    async def _discard_client(self, client: APIClient) -> None:
        if client in self._clients:
            self._clients.remove(client)
        try:
            await client.disconnect(force=True)
        except Exception:
            _LOGGER.debug("EP1 client disconnect after failure failed", exc_info=True)

    def _initial_targets(self) -> list[tuple[str, int]]:
        """Return hosts to probe without mDNS.

        With ``force_discovery``, only configured CLI/env hosts are used (cache
        ignored). Otherwise prefer the discovery cache, then configured hosts.
        """

        if self._force_discovery:
            return list(self._configured_hosts)
        cached = self._cache_targets()
        if cached:
            return cached
        return list(self._configured_hosts)

    def _resolved_noise_psk(self) -> str | None:
        if self._noise_psk is not None:
            return self._noise_psk
        psk, _source = resolve_ep1_noise_psk(
            cli_psk=self._cli_noise_psk,
            cache_path=self._discovery_cache_path,
        )
        return psk or None

    async def _resolve_targets(self) -> tuple[list[tuple[str, int]], bool]:
        """Return ``(targets, used_cache_only)``.

        Cache-first when rows exist and ``force_discovery`` is off. Otherwise
        configured hosts, then ESPHome mDNS for EP1 when zeroconf is enabled.
        Force discovery with no LAN hits falls back to the discovery cache so
        ``rediscover`` / ``refresh-discovery`` cannot wipe a working roster.
        """

        if self._force_discovery:
            if self._configured_hosts:
                return list(self._configured_hosts), False
            if self._zeroconf_discovery:
                try:
                    discovered = await self._zeroconf_discover_fn(timeout=self._zeroconf_timeout)
                    if discovered:
                        return list(discovered), False
                except Ep1DiscoveryError as exc:
                    _LOGGER.info("%s", exc)
            cached = self._cache_targets()
            if cached:
                _LOGGER.info(
                    "EP1 force discovery found no LAN targets; falling back to discovery cache (%d host(s))",
                    len(cached),
                )
                return cached, False
            return [], False

        base = self._initial_targets()
        used_cache_only = bool(self._cache_targets()) and bool(base)
        if used_cache_only:
            return base, True
        if base:
            return base, False
        if not self._zeroconf_discovery:
            return [], False
        try:
            discovered = await self._zeroconf_discover_fn(timeout=self._zeroconf_timeout)
        except Ep1DiscoveryError as exc:
            _LOGGER.info("%s", exc)
            return [], False
        return list(discovered), False


def _apply_entity_state_to_device(
    device: Ep1Device,
    state: EntityState,
    *,
    key_to_role: dict[int, str],
) -> None:
    role = key_to_role.get(state.key)
    if role is None:
        return
    if role == "occupancy":
        occupancy = _occupancy_from_state(state)
        if occupancy is not None:
            device.apply_entity_state(occupancy=occupancy)
        return
    value = _float_from_sensor_state(state)
    if value is None:
        return
    if role == "temperature":
        device.apply_entity_state(temperature_c=value)
    elif role == "humidity":
        device.apply_entity_state(humidity_pct=value)
    elif role == "illuminance":
        device.apply_entity_state(illuminance_lx=value)


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


def _is_ep1_service(info: AsyncServiceInfo) -> bool:
    """True when an ``_esphomelib._tcp`` record looks like an Everything Presence One."""
    props = info.properties or {}
    project = _mdns_prop_text(props, "project_name")
    friendly = _mdns_prop_text(props, "friendly_name")
    server = str(info.server or "").lower()
    name = str(info.name or "").lower()
    blob = f"{project} {friendly} {server} {name}"
    return any(marker in blob for marker in _EP1_NAME_MARKERS)


def _mdns_prop_text(properties: object, key: str) -> str:
    if not isinstance(properties, dict):
        return ""
    needle = key.lower()
    for raw_key, raw_val in properties.items():
        kn = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        if kn.lower() != needle:
            continue
        if raw_val is None:
            return ""
        if isinstance(raw_val, bytes):
            return raw_val.decode(errors="replace").lower()
        return str(raw_val).lower()
    return ""


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _occupancy_from_state(state: EntityState | None) -> bool | None:
    if not isinstance(state, BinarySensorState):
        return None
    if getattr(state, "missing_state", False):
        return None
    return bool(state.state)


def _pick_ep1_host_address(info: AsyncServiceInfo) -> str | None:
    raw_addrs = info.parsed_scoped_addresses()
    if not raw_addrs:
        return None
    for raw in raw_addrs:
        try:
            ip = ipaddress.ip_address(raw)
            if ip.version == 4:
                return str(ip)
        except ValueError:
            continue
    return raw_addrs[0]


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
