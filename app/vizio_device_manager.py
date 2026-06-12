"""Vizio SmartCast TV control (HTTPS REST on port 7345, WoL for power-on)."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

import aiohttp

from app import kasa_discovery_store
from app.device_manager import AlreadyInitializedError, NotInitializedError, SwitchDeviceManager
from app.rule_engine import SwitchDevice
from app.vizio_credentials import resolve_vizio_auth_token
from app.vizio_discovery import VizioDiscoveredHost, discover_vizio_hosts_ssdp
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

_WOL_POLL_INTERVAL_S = 2.0
_WOL_WAIT_DEADLINE_S = 60.0
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
        return device_id_for(self.host, self.port)


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
            powered = await self._client.get_power_on()
        except VizioSmartCastConnectionError:
            self._power_unknown = True
            return
        except VizioSmartCastAuthError:
            self._power_unknown = True
            return
        self._power_unknown = False
        self.set_power(powered)

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

    async def _wait_for_api(self) -> None:
        deadline = time.monotonic() + _WOL_WAIT_DEADLINE_S
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
            f"{_WOL_WAIT_DEADLINE_S:.0f}s after Wake-on-LAN"
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
            token, _source = self._resolve_token(endpoint.host)
            if not token:
                _LOGGER.info(
                    "Skipping Vizio TV %s — no auth token configured",
                    endpoint.device_id,
                )
                continue
            try:
                connected.append(await self._connect_endpoint(endpoint, token))
            except (VizioSmartCastAuthError, VizioSmartCastConnectionError) as exc:
                _LOGGER.warning(
                    "Cached/configured Vizio TV %s unreachable: %s",
                    endpoint.device_id,
                    exc,
                )
                failed.append(endpoint)

        if failed or self._force_discovery or not connected:
            used_discovery = True
            discovered = await discover_vizio_hosts_ssdp(timeout=self._discovery_timeout)
            for item in discovered:
                endpoint = VizioTvEndpoint(
                    host=item.host,
                    port=item.port,
                    display_name=item.name,
                    model=item.model or None,
                )
                if any(tv.identifier == endpoint.device_id for tv in connected):
                    continue
                token, _source = self._resolve_token(endpoint.host)
                if not token:
                    continue
                try:
                    connected.append(await self._connect_endpoint(endpoint, token))
                except (VizioSmartCastAuthError, VizioSmartCastConnectionError) as exc:
                    _LOGGER.warning(
                        "Discovered Vizio TV %s unreachable: %s",
                        endpoint.device_id,
                        exc,
                    )

        connected.sort(key=lambda tv: tv.preferred_label.lower())
        self._tvs = tuple(connected)
        self._id_to_tv = {tv.identifier: tv for tv in connected}
        self._initialized = True
        if not connected:
            self._last_discovery_source = None
        elif used_discovery and not targets:
            self._last_discovery_source = "discovery"
        elif used_discovery and failed:
            self._last_discovery_source = "discovery"
        else:
            self._last_discovery_source = "cache"

        if self._discovery_cache_path is not None:
            for tv in connected:
                ep = tv.endpoint
                kasa_discovery_store.upsert_vizio_tv(
                    self._discovery_cache_path,
                    host=ep.host,
                    port=ep.port,
                    display_name=tv.preferred_label,
                    model=ep.model,
                    mac=tv.mac,
                    diid=ep.diid,
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
        label = (endpoint.display_name or info.cast_name or info.model_name or "").strip()
        merged = VizioTvEndpoint(
            host=endpoint.host,
            port=endpoint.port,
            display_name=label or None,
            model=(endpoint.model or info.model_name or "").strip() or None,
            mac=endpoint.mac,
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

    def _initial_targets(self) -> list[VizioTvEndpoint]:
        out: list[VizioTvEndpoint] = []
        seen: set[str] = set()
        if self._discovery_cache_path is not None and not self._force_discovery:
            for host, port, display, model, mac, diid in kasa_discovery_store.load_vizio_tvs(
                self._discovery_cache_path
            ):
                device_id = device_id_for(host, port)
                if device_id in seen:
                    continue
                seen.add(device_id)
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
            device_id = device_id_for(host, port)
            if device_id in seen:
                continue
            seen.add(device_id)
            out.append(VizioTvEndpoint(host=host, port=port))
        return out

    def _resolve_token(self, host: str) -> tuple[str, str]:
        token, source = resolve_vizio_auth_token(
            host=host,
            cli_token=self._cli_auth_token,
            env_token=self._env_auth_token,
            cache_path=self._discovery_cache_path,
        )
        return token, source
