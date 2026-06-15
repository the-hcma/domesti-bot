"""Vizio SmartCast TV control (HTTPS REST on port 7345, WoL for power-on)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from app import device_discovery_store
from app.device_manager import AlreadyInitializedError, NotInitializedError, SwitchDeviceManager
from app.rule_engine import SwitchDevice
from app.vizio_credentials import (
    migrate_vizio_auth_token_host_to_mac,
    resolve_vizio_auth_token,
    vizio_device_id_from_parts,
)
from app.vizio_discovery import VizioDiscoveredHost, discover_vizio_hosts_ssdp
from app.vizio_mac import lookup_mac_via_arp, resolve_vizio_tv_ip, resolve_vizio_tv_mac
from app.vizio_smartcast_client import (
    DEFAULT_VIZIO_PORT,
    VizioSmartCastAuthError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
    device_id_for,
    parse_host_spec,
)
from app.vizio_wol import send_wake_on_lan

_LOGGER = logging.getLogger(__name__)

_API_PROBE_TIMEOUT_S = 2.0
_WOL_BOOTSTRAP_WAIT_DEADLINE_S = 12.0
_WOL_POLL_INTERVAL_S = 2.0
_WOL_WAIT_DEADLINE_S = 60.0


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
    __slots__ = ("_client", "_endpoint", "_mac", "_power_unknown")

    def __init__(
        self,
        endpoint: VizioTvEndpoint,
        client: VizioSmartCastClient,
        *,
        display_name: str | None = None,
        mac: str | None = None,
    ) -> None:
        super().__init__(endpoint.device_id, display_name=display_name)
        self._endpoint = endpoint
        self._client = client
        self._mac = (mac or endpoint.mac or "").strip() or None
        self._power_unknown = False

    @property
    def endpoint(self) -> VizioTvEndpoint:
        return self._endpoint

    @property
    def mac(self) -> str | None:
        return self._mac

    @property
    def preferred_label(self) -> str:
        if self.display_name:
            return self.display_name
        if self._endpoint.display_name:
            return self._endpoint.display_name
        if self._endpoint.model:
            return self._endpoint.model
        return self._endpoint.host

    async def refresh_power_state(self) -> None:
        try:
            active = await self._client.fetch_tv_active_state()
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
        try:
            await self._client.power_on()
        except VizioSmartCastConnectionError:
            if self._mac is None:
                raise
            await asyncio.to_thread(send_wake_on_lan, self._mac)
            await self._wait_for_api()
            await self._client.power_on()
        self._power_unknown = False
        self.set_power(True)

    def ui_power_state(self) -> str:
        """Cached on/off/unknown for the web UI and REPL listings."""
        if self._power_unknown:
            return "unknown"
        return "on" if self._on else "off"

    async def _wait_for_api(self, *, deadline_s: float | None = None) -> None:
        wait_deadline_s = _WOL_WAIT_DEADLINE_S if deadline_s is None else deadline_s
        deadline = time.monotonic() + wait_deadline_s
        host = self._endpoint.host
        port = self._endpoint.port
        while time.monotonic() < deadline:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=_API_PROBE_TIMEOUT_S,
                )
                writer.close()
                await writer.wait_closed()
                return
            except (TimeoutError, OSError):
                await asyncio.sleep(_WOL_POLL_INTERVAL_S)
        raise VizioSmartCastConnectionError(
            f"SmartCast API on {host}:{port} did not become reachable within "
            f"{wait_deadline_s:.0f}s after Wake-on-LAN"
        )


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
        targets = self._initial_targets()
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
                endpoint = VizioTvEndpoint(
                    host=item.host,
                    port=item.port,
                    display_name=item.name,
                    model=item.model or None,
                )
                if self._matches_known_tv(endpoint, connected):
                    continue
                token, _source = self._resolve_token(endpoint)
                if not token:
                    continue
                tv, _unreachable = await self._connect_target(endpoint, token)
                if tv is not None:
                    connected.append(tv)

        connected.sort(key=lambda tv: tv.preferred_label.lower())
        self._tvs = tuple(connected)
        self._id_to_tv = {tv.identifier: tv for tv in connected}
        self._initialized = True
        if not connected:
            self._last_discovery_source = None
        elif used_discovery and (self._force_discovery or not targets or failed):
            self._last_discovery_source = "discovery"
        else:
            self._last_discovery_source = "cache"

        if self._discovery_cache_path is not None:
            for tv in connected:
                ep = tv.endpoint
                device_discovery_store.upsert_vizio_tv(
                    self._discovery_cache_path,
                    host=ep.host,
                    port=ep.port,
                    display_name=tv.preferred_label,
                    model=ep.model,
                    mac=tv.mac,
                    diid=ep.diid,
                )
                if tv.mac:
                    migrate_vizio_auth_token_host_to_mac(
                        self._discovery_cache_path,
                        host=ep.host,
                        mac=tv.mac,
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

    async def turn_off(self, identifier: str) -> None:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        await tv.turn_off()

    async def turn_on(self, identifier: str) -> None:
        tv = self.get_device_by_id(identifier)
        if tv is None:
            raise KeyError(identifier)
        await tv.turn_on()

    async def _connect_endpoint(
        self,
        endpoint: VizioTvEndpoint,
        token: str,
    ) -> VizioTvDevice:
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
            mac=merged.mac,
        )
        await tv.refresh_power_state()
        return tv

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
                return await self._offline_tv(endpoint, token), endpoint
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
            return await self._offline_tv(endpoint, token), endpoint
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

    def _initial_targets(self) -> list[VizioTvEndpoint]:
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
        return out

    async def _offline_tv(self, endpoint: VizioTvEndpoint, token: str) -> VizioTvDevice:
        """Return a cached TV tile when SmartCast is unreachable at bootstrap."""
        _LOGGER.info(
            "Vizio TV %s bootstrap: SmartCast unreachable at %s:%s; "
            "probing with Wake-on-LAN",
            endpoint.device_id,
            endpoint.host,
            endpoint.port,
        )
        endpoint = await self._endpoint_with_resolved_mac(endpoint)
        probed = await self._wake_and_probe_tv(endpoint, token)
        if probed is not None:
            return probed
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
            mac=endpoint.mac,
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
            if endpoint.mac and tv.mac and endpoint.mac == tv.mac:
                return True
            if (
                tv.endpoint.host == endpoint.host
                and tv.endpoint.port == endpoint.port
            ):
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

    async def _wake_and_probe_tv(
        self,
        endpoint: VizioTvEndpoint,
        token: str,
    ) -> VizioTvDevice | None:
        """WoL once at bootstrap, poll SmartCast, and connect when the TV answers."""
        mac = endpoint.mac
        if mac is None:
            return None
        endpoint = await self._relocate_endpoint(endpoint, force_arp=True)
        _LOGGER.info(
            "Vizio TV %s bootstrap: sent Wake-on-LAN to %s; waiting up to %.0fs "
            "for SmartCast at %s:%s",
            endpoint.device_id,
            mac,
            _WOL_BOOTSTRAP_WAIT_DEADLINE_S,
            endpoint.host,
            endpoint.port,
        )
        await asyncio.to_thread(send_wake_on_lan, mac)
        client = VizioSmartCastClient(
            endpoint.host,
            port=endpoint.port,
            auth_token=token,
            session=self._session,
        )
        label = (endpoint.display_name or endpoint.model or endpoint.host).strip()
        probe = VizioTvDevice(
            endpoint,
            client,
            display_name=label or None,
            mac=mac,
        )
        try:
            await probe._wait_for_api(deadline_s=_WOL_BOOTSTRAP_WAIT_DEADLINE_S)
        except VizioSmartCastConnectionError:
            _LOGGER.info(
                "Vizio TV %s did not answer SmartCast after Wake-on-LAN; treating as off",
                endpoint.device_id,
            )
            return None
        endpoint = await self._relocate_endpoint(endpoint, force_arp=True)
        try:
            return await self._connect_endpoint(endpoint, token)
        except VizioSmartCastAuthError:
            _LOGGER.warning(
                "Vizio TV %s auth rejected after Wake-on-LAN probe; treating as off",
                endpoint.device_id,
            )
            return None
        except VizioSmartCastConnectionError:
            _LOGGER.info(
                "Vizio TV %s unreachable after Wake-on-LAN probe; treating as off",
                endpoint.device_id,
            )
            return None
