"""Vizio SmartCast TV control (HTTPS REST on port 7345)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from app import device_discovery_store
from app.db.secrets import (
    SecretsDecryptError,
    load_vizio_auth_hosts_from_db,
    load_vizio_auth_token_from_db,
)
from app.device_display import format_device_display
from app.device_enums import DeviceConditionState
from app.device_mac import mac_alive_on_lan
from app.device_manager import AlreadyInitializedError, NotInitializedError, SwitchDeviceManager
from app.rule_engine import SwitchDevice
from app.vizio_credentials import (
    migrate_vizio_auth_token_host_to_mac,
    resolve_vizio_auth_token,
    vizio_device_id_from_parts,
)
from app.vizio_discovery import discover_vizio_hosts_ssdp
from app.vizio_mac import (
    lookup_ip_via_arp_for_mac,
    lookup_mac_via_arp,
    resolve_vizio_tv_ip,
    try_normalize_mac,
)
from app.vizio_smartcast_client import (
    DEFAULT_VIZIO_PORT,
    VizioSmartCastAuthError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
    device_id_for,
    parse_host_spec,
    resolve_vizio_tv_mac,
)

_LOGGER = logging.getLogger(__name__)

_API_PROBE_TIMEOUT_S = 2.0


def configured_vizio_host_specs(
    *,
    cli_hosts: list[str] | None,
    env_hosts: str | None,
) -> list[tuple[str, int]]:
    """Merge repeatable CLI hosts and ``VIZIO_HOSTS`` env (comma-separated)."""
    specs: list[str] = []
    if cli_hosts:
        specs.extend(h.strip() for h in cli_hosts if h and h.strip())
    env_raw = (env_hosts or os.environ.get("VIZIO_HOSTS") or "").strip()
    if env_raw:
        specs.extend(part.strip() for part in env_raw.split(",") if part.strip())
    out: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()
    for raw in specs:
        try:
            host, port = parse_host_spec(raw)
        except ValueError as exc:
            _LOGGER.warning("Skipping invalid VIZIO host spec %r: %s", raw, exc)
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@dataclass(frozen=True, slots=True)
class VizioTvEndpoint:
    host: str
    port: int
    display_name: str | None = None
    model: str | None = None
    mac: str | None = None
    diid: str | None = None

    @property
    def device_id(self) -> str:
        return vizio_device_id_from_parts(mac=self.mac, host=self.host, port=self.port)


class VizioTvDevice(SwitchDevice):
    __slots__ = ("_client", "_endpoint", "_mac_address", "_power_unknown")

    def __init__(
        self,
        endpoint: VizioTvEndpoint,
        client: VizioSmartCastClient,
        *,
        display_name: str | None = None,
        mac_address: str,
    ) -> None:
        super().__init__(endpoint.device_id, display_name=display_name)
        self._endpoint = endpoint
        self._client = client
        self._mac_address = mac_address.strip()
        self._power_unknown = False

    @property
    def endpoint(self) -> VizioTvEndpoint:
        return self._endpoint

    @property
    def mac_address(self) -> str:
        return self._mac_address

    @property
    def preferred_label(self) -> str:
        if self.display_name:
            return self.display_name
        if self._endpoint.display_name:
            return self._endpoint.display_name
        if self._endpoint.model:
            return self._endpoint.model
        return self._endpoint.host

    async def refresh_power_state(self, *, poll: bool = False) -> None:
        try:
            active = await self._client.fetch_tv_active_state(poll=poll)
        except VizioSmartCastAuthError:
            self._power_unknown = True
            return
        except VizioSmartCastConnectionError:
            self._power_unknown = False
            self.set_power(False)
            return
        self._power_unknown = False
        self.set_power(active)

    async def turn_off(self) -> None:
        self.require_responsive()
        # Quick Start standby keeps SmartCast reachable. Sending power_off
        # while already off can wake the display (seen on Kitchen TV when
        # away-shutdown dispatched turn_off for every listed device).
        if not self._power_unknown and not self._on:
            _LOGGER.debug(
                "Skipping power_off for %s; already off",
                self.identifier,
            )
            return
        try:
            await self._client.power_off()
        except VizioSmartCastConnectionError:
            _LOGGER.info(
                "SmartCast unreachable for %s during power_off; treating as off",
                self.identifier,
            )
        self._power_unknown = False
        self.set_power(False)

    async def turn_on(self) -> None:
        self.require_responsive()
        if not self._power_unknown and self._on:
            _LOGGER.debug(
                "Skipping power_on for %s; already on",
                self.identifier,
            )
            return
        await self._client.power_on()
        self._power_unknown = False
        self.set_power(True)

    async def flip(self) -> str:
        if self._power_unknown:
            await self.turn_off()
            return "on=False"
        if self.power_state == DeviceConditionState.ON:
            await self.turn_off()
            return "on=False"
        await self.turn_on()
        return "on=True"

    def ui_power_state(self) -> str:
        """Cached on/off/unknown for the web UI and REPL listings."""
        if self.unresponsive or self._power_unknown:
            return "unknown"
        return "on" if self._on else "off"


class VizioDeviceManager(SwitchDeviceManager[VizioTvDevice]):
    """Cache-first SmartCast manager for one or more TVs."""

    def __init__(
        self,
        *,
        configured_hosts: list[tuple[str, int]],
        discovery_cache_path: Path | None,
        cli_auth_token: str | None = None,
        env_auth_token: str | None = None,
        force_discovery: bool = False,
        discovery_timeout: float = 5.0,
    ) -> None:
        self._configured_hosts = configured_hosts
        self._discovery_cache_path = discovery_cache_path
        self._cli_auth_token = cli_auth_token
        self._env_auth_token = env_auth_token
        self._force_discovery = force_discovery
        self._discovery_timeout = discovery_timeout
        self._session: aiohttp.ClientSession | None = None
        self._tvs: tuple[VizioTvDevice, ...] = ()
        self._id_to_tv: dict[str, VizioTvDevice] = {}
        self._initialized = False
        self._last_discovery_source: str | None = None

    @property
    def last_discovery_source(self) -> str | None:
        return self._last_discovery_source

    @property
    def tvs(self) -> tuple[VizioTvDevice, ...]:
        if not self._initialized:
            raise NotInitializedError("VizioDeviceManager.fetch() has not completed")
        return self._tvs

    def get_device_by_id(self, device_id: str) -> VizioTvDevice | None:
        if not self._initialized:
            raise NotInitializedError("VizioDeviceManager.fetch() has not completed")
        return self._id_to_tv.get(device_id)

    async def disconnect(self) -> None:
        for tv in self._tvs:
            await tv._client.aclose()
        self._tvs = ()
        self._id_to_tv = {}
        self._initialized = False
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def fetch(self) -> None:
        if self._initialized:
            raise AlreadyInitializedError("VizioDeviceManager.fetch() already ran")
        connector = aiohttp.TCPConnector(ssl=False)
        self._session = aiohttp.ClientSession(connector=connector)
        targets = await self._initial_targets()
        used_discovery = False
        connected: list[VizioTvDevice] = []
        failed: list[VizioTvEndpoint] = []

        for endpoint in targets:
            token, _source = self._resolve_token(endpoint)
            if not token:
                _LOGGER.info(
                    "Skipping Vizio TV %s — no auth token configured",
                    endpoint.device_id,
                )
                continue
            tv, unreachable = await self._connect_target(endpoint, token)
            if tv is not None:
                connected.append(tv)
            if unreachable is not None:
                failed.append(unreachable)

        if self._should_run_ssdp(connected=connected, failed=failed):
            used_discovery = True
            discovered = await discover_vizio_hosts_ssdp(timeout=self._discovery_timeout)
            for item in discovered:
                cached_mac = self._cached_mac_for_host(item.host, item.port)
                if cached_mac is None:
                    cached_mac = await asyncio.to_thread(lookup_mac_via_arp, item.host)
                endpoint = VizioTvEndpoint(
                    host=item.host,
                    port=item.port,
                    display_name=item.name,
                    model=item.model or None,
                    mac=cached_mac,
                )
                if self._matches_known_tv(endpoint, connected):
                    continue
                tv = await self._connect_ssdp_target(endpoint, connected)
                if tv is not None:
                    connected.append(tv)

        connected.sort(key=lambda tv: tv.preferred_label.lower())
        self._tvs = tuple(connected)
        self._id_to_tv = {tv.identifier: tv for tv in connected}
        self._initialized = True
        if not connected:
            self._last_discovery_source = "discovery" if used_discovery else None
        elif used_discovery and (self._force_discovery or not targets or failed):
            self._last_discovery_source = "discovery"
        else:
            self._last_discovery_source = "cache"

        if self._discovery_cache_path is not None:
            rows = [
                (
                    tv.endpoint.host,
                    tv.endpoint.port,
                    tv.preferred_label,
                    tv.endpoint.model,
                    tv.mac_address,
                    tv.endpoint.diid,
                )
                for tv in connected
            ]
            if self._force_discovery:
                device_discovery_store.save_vizio_tvs(self._discovery_cache_path, rows)
                for tv in connected:
                    ep = tv.endpoint
                    migrate_vizio_auth_token_host_to_mac(
                        self._discovery_cache_path,
                        host=ep.host,
                        mac=tv.mac_address,
                    )
            else:
                for tv in connected:
                    ep = tv.endpoint
                    device_discovery_store.upsert_vizio_tv(
                        self._discovery_cache_path,
                        host=ep.host,
                        port=ep.port,
                        display_name=tv.preferred_label,
                        model=ep.model,
                        mac=tv.mac_address,
                        diid=ep.diid,
                    )
                    migrate_vizio_auth_token_host_to_mac(
                        self._discovery_cache_path,
                        host=ep.host,
                        mac=tv.mac_address,
                    )

    async def is_off(self, identifier: str) -> bool:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        return tv.is_off

    async def is_on(self, identifier: str) -> bool:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        return tv.is_on

    async def rediscover(self) -> None:
        """Rerun SSDP discovery while keeping cached TVs; ``fetch`` stays cache-first."""
        await self.disconnect()
        previous = self._force_discovery
        self._force_discovery = True
        try:
            await self.fetch()
        finally:
            self._force_discovery = previous

    async def reload_from_cache(self) -> bool:
        """Replace the in-memory TV map from SQLite only (never SSDP).

        Reconnects every cached endpoint that has an auth token. Does not
        upsert the discovery table. On auth/connect failure for any token'd
        row, keeps the prior map and returns ``False``.
        """

        if not self._initialized:
            _LOGGER.debug("Vizio reload_from_cache: manager not initialized")
            return False
        if self._discovery_cache_path is None:
            _LOGGER.debug("Vizio reload_from_cache: no discovery cache path")
            return False
        rows = device_discovery_store.load_vizio_tvs(self._discovery_cache_path)
        if not rows:
            for tv in self._tvs:
                with contextlib.suppress(Exception):
                    await tv._client.aclose()
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None
            self._tvs = ()
            self._id_to_tv = {}
            self._last_discovery_source = "cache"
            _LOGGER.info("Vizio reload_from_cache: empty cache; cleared device map")
            return True

        targets: list[VizioTvEndpoint] = []
        for host, port, display, model, mac, diid in rows:
            targets.append(
                VizioTvEndpoint(
                    host=host,
                    port=port,
                    display_name=display,
                    model=model,
                    mac=mac,
                    diid=diid,
                )
            )
        token_targets = [ep for ep in targets if self._resolve_token(ep)[0]]
        if not token_targets:
            _LOGGER.info("Vizio reload_from_cache: no cached TVs with auth tokens; keeping prior device map")
            return False

        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False)
            self._session = aiohttp.ClientSession(connector=connector)

        previous_tvs = self._tvs
        previous_ids = self._id_to_tv
        connected: list[VizioTvDevice] = []
        try:
            for endpoint in token_targets:
                token, _source = self._resolve_token(endpoint)
                tv, _unreachable = await self._connect_target(endpoint, token)
                if tv is None:
                    raise RuntimeError(f"Vizio reload_from_cache: failed to connect {endpoint.device_id}")
                connected.append(tv)
        except Exception:
            for tv in connected:
                with contextlib.suppress(Exception):
                    await tv._client.aclose()
            self._tvs = previous_tvs
            self._id_to_tv = previous_ids
            _LOGGER.warning(
                "Vizio reload_from_cache: reconnect failed; keeping prior device map",
                exc_info=True,
            )
            return False

        for tv in previous_tvs:
            with contextlib.suppress(Exception):
                await tv._client.aclose()
        connected.sort(key=lambda tv: tv.preferred_label.lower())
        self._tvs = tuple(connected)
        self._id_to_tv = {tv.identifier: tv for tv in connected}
        self._last_discovery_source = "cache"
        _LOGGER.info(
            "Vizio reload_from_cache: replaced device map from cache (%d TV(s))",
            len(self._tvs),
        )
        return True

    async def turn_off(self, identifier: str) -> None:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        await tv.turn_off()

    async def turn_on(self, identifier: str) -> None:
        await self._device_for(identifier).turn_on()

    def _device_for(self, identifier: str) -> VizioTvDevice:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        return tv

    async def _connect_endpoint(
        self,
        endpoint: VizioTvEndpoint,
        token: str,
    ) -> VizioTvDevice | None:
        client = VizioSmartCastClient(
            endpoint.host,
            port=endpoint.port,
            auth_token=token,
            session=self._session,
        )
        info = await client.fetch_deviceinfo()
        mac = endpoint.mac or info.mac
        if mac is None:
            mac = await resolve_vizio_tv_mac(client, host=endpoint.host)
        if mac is None:
            _LOGGER.warning(
                "Skipping Vizio TV at %s:%s — MAC address required",
                endpoint.host,
                endpoint.port,
            )
            return None
        label = (endpoint.display_name or info.cast_name or info.model_name or "").strip()
        merged = VizioTvEndpoint(
            host=endpoint.host,
            port=endpoint.port,
            display_name=label or None,
            model=(endpoint.model or info.model_name or "").strip() or None,
            mac=mac,
            diid=(endpoint.diid or info.diid or "").strip() or None,
        )
        tv = VizioTvDevice(
            merged,
            client,
            display_name=label or None,
            mac_address=mac,
        )
        await tv.refresh_power_state()
        return tv

    async def _connect_ssdp_target(
        self,
        endpoint: VizioTvEndpoint,
        connected: list[VizioTvDevice],
    ) -> VizioTvDevice | None:
        """Connect an SSDP host, including leftover MAC-keyed SmartCast tokens.

        DIAL XML has no MAC. Tokens live under ``vizio_auth:<mac>``, so a cache
        miss plus an ARP miss would otherwise skip a TV the CLI just found.
        """

        token, _source = self._resolve_token(endpoint)
        if token:
            tv, _unreachable = await self._connect_target(endpoint, token)
            return tv
        used_macs = {tv.mac_address for tv in connected if tv.mac_address}
        leftovers = self._leftover_mac_auth_tokens(exclude_macs=used_macs)
        if not leftovers:
            _LOGGER.info(
                "Skipping Vizio TV %s — no auth token configured",
                endpoint.device_id,
            )
            return None
        probe = VizioTvEndpoint(
            host=endpoint.host,
            port=endpoint.port,
            display_name=endpoint.display_name,
            model=endpoint.model,
            mac=None,
            diid=endpoint.diid,
        )
        for expected_mac, leftover_token in leftovers:
            tv, _unreachable = await self._connect_target(probe, leftover_token)
            if tv is None:
                continue
            if tv.mac_address == expected_mac:
                return tv
            with contextlib.suppress(Exception):
                await tv._client.aclose()
        _LOGGER.info(
            "Skipping Vizio TV %s — leftover SmartCast tokens did not match",
            endpoint.device_id,
        )
        return None

    async def _connect_target(
        self,
        endpoint: VizioTvEndpoint,
        token: str,
    ) -> tuple[VizioTvDevice | None, VizioTvEndpoint | None]:
        """Connect one TV target, or return an offline tile when unreachable."""
        endpoint = await self._relocate_endpoint(endpoint)
        if not await self._smartcast_port_open(endpoint):
            relocated = await self._relocate_endpoint(endpoint, force_arp=True)
            if relocated.host != endpoint.host and await self._smartcast_port_open(relocated):
                endpoint = relocated
            else:
                if relocated.host != endpoint.host:
                    endpoint = relocated
                _LOGGER.warning(
                    "Vizio TV %s unreachable: SmartCast port closed on %s:%s",
                    endpoint.device_id,
                    endpoint.host,
                    endpoint.port,
                )
                return await self._unreachable_tv(endpoint, token), endpoint
        try:
            return await self._connect_endpoint(endpoint, token), None
        except VizioSmartCastConnectionError as exc:
            relocated = await self._relocate_endpoint(endpoint, force_arp=True)
            if relocated.host != endpoint.host:
                try:
                    return await self._connect_endpoint(relocated, token), None
                except (VizioSmartCastAuthError, VizioSmartCastConnectionError):
                    endpoint = relocated
            _LOGGER.warning(
                "Vizio TV %s unreachable: %s",
                endpoint.device_id,
                exc,
            )
            return await self._unreachable_tv(endpoint, token), endpoint
        except VizioSmartCastAuthError as exc:
            _LOGGER.warning(
                "Vizio TV %s auth rejected: %s",
                endpoint.device_id,
                exc,
            )
            return None, endpoint

    async def _endpoint_with_resolved_mac(
        self,
        endpoint: VizioTvEndpoint,
    ) -> VizioTvEndpoint:
        """Attach a MAC from ARP when the cached endpoint does not have one."""
        mac = endpoint.mac
        if mac is not None:
            return endpoint
        mac = await asyncio.to_thread(lookup_mac_via_arp, endpoint.host)
        if mac is None:
            return endpoint
        return VizioTvEndpoint(
            host=endpoint.host,
            port=endpoint.port,
            display_name=endpoint.display_name,
            model=endpoint.model,
            mac=mac,
            diid=endpoint.diid,
        )

    async def _arp_visible_auth_targets(self) -> list[VizioTvEndpoint]:
        """Cached-auth TVs whose MAC still appears in the local ARP table."""

        if self._discovery_cache_path is None:
            return []
        out: list[VizioTvEndpoint] = []
        for key in load_vizio_auth_hosts_from_db(self._discovery_cache_path):
            mac = try_normalize_mac(key)
            if mac is None:
                continue
            ip = await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac)
            if ip is None:
                continue
            out.append(VizioTvEndpoint(host=ip, port=DEFAULT_VIZIO_PORT, mac=mac))
        return out

    def _cached_mac_for_host(self, host: str, port: int) -> str | None:
        """Return a cached MAC for ``host:port`` so mac-keyed tokens resolve during SSDP."""

        if self._discovery_cache_path is None:
            return None
        for cached_host, cached_port, _display, _model, mac, _diid in device_discovery_store.load_vizio_tvs(
            self._discovery_cache_path
        ):
            if cached_host == host and cached_port == port:
                return mac
        return None

    async def _initial_targets(self) -> list[VizioTvEndpoint]:
        out: list[VizioTvEndpoint] = []
        seen_ids: set[str] = set()
        seen_hosts: set[tuple[str, int]] = set()
        if self._discovery_cache_path is not None:
            for host, port, display, model, mac, diid in device_discovery_store.load_vizio_tvs(
                self._discovery_cache_path
            ):
                host_key = (host, port)
                if host_key in seen_hosts:
                    continue
                device_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
                if device_id in seen_ids:
                    continue
                seen_ids.add(device_id)
                seen_hosts.add(host_key)
                out.append(
                    VizioTvEndpoint(
                        host=host,
                        port=port,
                        display_name=display,
                        model=model,
                        mac=mac,
                        diid=diid,
                    )
                )
        for host, port in self._configured_hosts:
            host_key = (host, port)
            if host_key in seen_hosts:
                continue
            device_id = device_id_for(host, port)
            if device_id in seen_ids:
                continue
            seen_ids.add(device_id)
            seen_hosts.add(host_key)
            out.append(VizioTvEndpoint(host=host, port=port))
        # Auth MACs remain after vizio_known_tvs is emptied (rediscover ARP miss).
        for endpoint in await self._arp_visible_auth_targets():
            host_key = (endpoint.host, endpoint.port)
            if host_key in seen_hosts:
                continue
            device_id = endpoint.device_id
            if device_id in seen_ids:
                continue
            seen_ids.add(device_id)
            seen_hosts.add(host_key)
            out.append(endpoint)
        return out

    def _leftover_mac_auth_tokens(self, *, exclude_macs: set[str]) -> list[tuple[str, str]]:
        """Return ``(mac, token)`` for leftover SmartCast secrets not already connected."""

        if self._discovery_cache_path is None:
            return []
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for key in load_vizio_auth_hosts_from_db(self._discovery_cache_path):
            mac = try_normalize_mac(key)
            if mac is None or mac in exclude_macs or mac in seen:
                continue
            try:
                token = load_vizio_auth_token_from_db(
                    self._discovery_cache_path,
                    mac=mac,
                    host=None,
                )
            except SecretsDecryptError:
                continue
            if not token:
                continue
            seen.add(mac)
            out.append((mac, token))
        return out

    async def _mac_alive_on_lan(self, endpoint: VizioTvEndpoint) -> bool:
        """True when the TV's MAC (or host ARP neighbor) is still on the LAN."""

        return await asyncio.to_thread(mac_alive_on_lan, mac=endpoint.mac, host=endpoint.host)

    async def _offline_tv(self, endpoint: VizioTvEndpoint, token: str) -> VizioTvDevice | None:
        """Return a cached off tile when SmartCast is unreachable at bootstrap."""
        _LOGGER.info(
            "Vizio TV %s bootstrap: SmartCast unreachable at %s:%s; registering as off",
            endpoint.device_id,
            endpoint.host,
            endpoint.port,
        )
        endpoint = await self._endpoint_with_resolved_mac(endpoint)
        if endpoint.mac is None:
            _LOGGER.warning(
                "Skipping offline Vizio TV at %s:%s — MAC address required",
                endpoint.host,
                endpoint.port,
            )
            return None
        client = VizioSmartCastClient(
            endpoint.host,
            port=endpoint.port,
            auth_token=token,
            session=self._session,
        )
        label = (endpoint.display_name or endpoint.model or endpoint.host).strip()
        tv = VizioTvDevice(
            endpoint,
            client,
            display_name=label or None,
            mac_address=endpoint.mac,
        )
        tv.set_power(False)
        return tv

    def _matches_known_tv(
        self,
        endpoint: VizioTvEndpoint,
        connected: list[VizioTvDevice],
    ) -> bool:
        """True when ``endpoint`` is already represented in ``connected``."""
        for tv in connected:
            if tv.identifier == endpoint.device_id:
                return True
            if endpoint.mac and endpoint.mac == tv.mac_address:
                return True
            if tv.endpoint.host == endpoint.host and tv.endpoint.port == endpoint.port:
                return True
        return False

    async def _relocate_endpoint(
        self,
        endpoint: VizioTvEndpoint,
        *,
        force_arp: bool = False,
    ) -> VizioTvEndpoint:
        """Refresh ``host`` from MAC via ARP when the TV's DHCP address changed."""
        if not endpoint.mac:
            return endpoint
        if not force_arp and endpoint.host:
            ip = await resolve_vizio_tv_ip(mac=endpoint.mac, fallback_host=endpoint.host)
        else:
            ip = await resolve_vizio_tv_ip(mac=endpoint.mac, fallback_host=None)
        if ip is None or ip == endpoint.host:
            return endpoint
        _LOGGER.info(
            "Relocated Vizio TV %s from %s to %s via MAC",
            endpoint.device_id,
            endpoint.host,
            ip,
        )
        return VizioTvEndpoint(
            host=ip,
            port=endpoint.port,
            display_name=endpoint.display_name,
            model=endpoint.model,
            mac=endpoint.mac,
            diid=endpoint.diid,
        )

    def _resolve_token(self, endpoint: VizioTvEndpoint) -> tuple[str, str]:
        token, source = resolve_vizio_auth_token(
            mac=endpoint.mac,
            host=endpoint.host,
            cli_token=self._cli_auth_token,
            env_token=self._env_auth_token,
            cache_path=self._discovery_cache_path,
        )
        return token, source

    def _should_run_ssdp(
        self,
        *,
        connected: list[VizioTvDevice],
        failed: list[VizioTvEndpoint],
    ) -> bool:
        """Run LAN discovery for new TVs, not on every cache miss with a known MAC."""
        if self._force_discovery:
            return True
        if not connected:
            return True
        if not failed:
            return False
        return any(endpoint.mac is None for endpoint in failed)

    async def _smartcast_port_open(self, endpoint: VizioTvEndpoint) -> bool:
        """Return whether TCP ``host:port`` accepts a connection within the probe budget."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(endpoint.host, endpoint.port),
                timeout=_API_PROBE_TIMEOUT_S,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except (TimeoutError, OSError):
            return False

    async def _unreachable_tv(
        self,
        endpoint: VizioTvEndpoint,
        token: str,
    ) -> VizioTvDevice | None:
        """Keep an ARP-visible TV as unresponsive; drop it on rediscover if ARP misses."""

        endpoint = await self._endpoint_with_resolved_mac(endpoint)
        alive = await self._mac_alive_on_lan(endpoint)
        if self._force_discovery and not alive:
            _LOGGER.info(
                "Dropping Vizio TV %s — SmartCast unreachable and MAC not on the LAN",
                format_device_display(endpoint.device_id, endpoint.display_name),
            )
            return None
        tv = await self._offline_tv(endpoint, token)
        if tv is None:
            return None
        if alive:
            tv.set_unresponsive(True)
            _LOGGER.info(
                "Vizio TV %s is on the LAN (ARP) but SmartCast is silent; keeping as unresponsive",
                format_device_display(tv.identifier, tv.preferred_label),
            )
        return tv
