"""Tailwind / GoTailwind garage controller integration (see https://pypi.org/project/gotailwind/).

**Local Control Key (``token``)**

The HTTP API expects a **6-digit Local Control Key**, issued by Tailwind (not your login
password). Pass it as ``token=`` or ``TAILWIND_TOKEN`` in the environment.

Where to get or rotate it:

1. Log in to the Tailwind web dashboard: https://web.gotailwind.com
2. Open the **Local Control Key** page and copy the code.
3. If the key may be compromised, generate a new one on that page (keep the device **online**
   while creating a new key).
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import ipaddress
import logging
import os
import sys
from pathlib import Path
from typing import cast

# ``gotailwind`` depends on ``backoff``, which still calls ``asyncio.iscoroutinefunction``.
# That API is deprecated in Python 3.14+ (removed in 3.16); delegate to ``inspect``.
if sys.version_info >= (3, 14):
    asyncio.iscoroutinefunction = inspect.iscoroutinefunction  # type: ignore[method-assign]

from gotailwind import Tailwind
from gotailwind.const import TailwindDoorOperationCommand, TailwindDoorState
from gotailwind.exceptions import TailwindDoorAlreadyInStateError
from gotailwind.models import TailwindDoor
from zeroconf import ServiceStateChange, Zeroconf
from zeroconf.asyncio import AsyncServiceBrowser, AsyncServiceInfo, AsyncZeroconf

from app import device_discovery_store
from app.device_mac import lookup_mac_via_arp
from app.device_manager import AlreadyInitializedError, DoorDeviceManager, NotInitializedError
from app.rule_engine import DoorDevice

_LOGGER = logging.getLogger(__name__)

# Same browse type as ``tailwind scan`` (gotailwind CLI).
_MDNS_HTTP_TCP_LOCAL = "_http._tcp.local."


class TailwindDiscoveryError(RuntimeError):
    """No Tailwind unit responded on the LAN within the discovery window."""

    pass


def _pick_tailwind_host_address(info: AsyncServiceInfo) -> str | None:
    """Choose an IP/host string from an mDNS record; mirror CLI ``tailwind scan`` filtering."""
    if info.properties is None:
        return None
    server = str(info.server)
    if not server.lower().startswith("tailwind-"):
        return None
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


async def discover_tailwind_host(*, timeout: float = 12.0) -> str:
    """Return the address of the first Tailwind iQ3 found via mDNS (IPv4 preferred).

    Matches the logic used by ``tailwind scan`` from ``gotailwind[cli]``.
    """
    queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
    zc = AsyncZeroconf()
    pending: set[asyncio.Task[None]] = set()

    async def resolve_service(service_type: str, name: str) -> None:
        info = AsyncServiceInfo(service_type, name)
        await info.async_request(zc.zeroconf, 3000)
        addr = _pick_tailwind_host_address(info)
        if addr is None:
            return
        try:
            queue.put_nowait(addr)
        except asyncio.QueueFull:
            pass

    def on_service_state_change(
        zeroconf: Zeroconf,
        service_type: str,
        name: str,
        state_change: ServiceStateChange,
    ) -> None:
        # Handle Updated as well as Added: TXT/SRV records sometimes arrive after the first browse.
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
        _MDNS_HTTP_TCP_LOCAL,
        handlers=[on_service_state_change],
    )
    try:
        return await asyncio.wait_for(queue.get(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TailwindDiscoveryError(
            f"No Tailwind device found on the LAN within {timeout}s "
            "(set TAILWIND_HOST or run on the same subnet as the controller)."
        ) from exc
    finally:
        await browser.async_cancel()
        await zc.async_close()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class GotailwindDevice(DoorDevice):
    """One garage door on a Tailwind controller (``open`` / ``close`` only)."""

    __slots__ = ("_door", "_door_index", "_door_key", "_mac_address", "_reported_state", "_tailwind")

    def __init__(
        self,
        identifier: str,
        tailwind: Tailwind,
        door: int | str,
        *,
        reported_state: TailwindDoorState,
        door_index: int,
        display_name: str | None = None,
        door_key: str | None = None,
        mac_address: str,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._tailwind = tailwind
        self._door = door
        self._door_index = door_index
        self._door_key = (door_key or identifier).strip()
        self._mac_address = mac_address
        self._reported_state = reported_state

    def _sync_reported_state(self, state: TailwindDoorState) -> None:
        self._reported_state = state

    async def close(self) -> None:
        # ``gotailwind`` raises ``TailwindDoorAlreadyInStateError`` when
        # the door is already closed (or already in any commanded state).
        # Our app contract treats close/open as idempotent — a user
        # clicking "Close" on a closed door, or "Turn off / pause / close everything"
        # while some doors are already shut, should succeed. Pin the
        # cached ``_reported_state`` to the target so the next refresh
        # reflects what's actually true.
        try:
            door = await self._tailwind.operate(
                door=self._door,
                operation=TailwindDoorOperationCommand.CLOSE,
            )
        except TailwindDoorAlreadyInStateError:
            self._reported_state = TailwindDoorState.CLOSED
            return
        self._reported_state = door.state

    @property
    def door_index(self) -> int:
        return self._door_index

    @property
    def door_key(self) -> str:
        """Vendor door identifier (secondary id when MAC compound form is used)."""

        return self._door_key

    @property
    def is_closed(self) -> bool:
        return self._reported_state == TailwindDoorState.CLOSED

    @property
    def is_open(self) -> bool:
        return self._reported_state == TailwindDoorState.OPEN

    @property
    def mac_address(self) -> str:
        return self._mac_address

    async def open(self) -> None:
        # Symmetric with :meth:`close`: swallow
        # ``TailwindDoorAlreadyInStateError`` so a stale "closed" tile
        # double-click (or a bulk "open all" hitting a door that's
        # already open) succeeds as a no-op instead of 500ing.
        try:
            door = await self._tailwind.operate(
                door=self._door,
                operation=TailwindDoorOperationCommand.OPEN,
            )
        except TailwindDoorAlreadyInStateError:
            self._reported_state = TailwindDoorState.OPEN
            return
        self._reported_state = door.state


class GotailwindDeviceManager(DoorDeviceManager[GotailwindDevice]):
    """One manager per Tailwind unit.

    * ``host``: explicit IP/hostname, or ``None`` to use ``TAILWIND_HOST``, or mDNS discovery
      when that env var is unset (same mechanism as ``tailwind scan``).
    * ``token``: 6-digit **Local Control Key** from https://web.gotailwind.com (see module
      docstring). Often supplied via ``TAILWIND_TOKEN``.
    """

    def __init__(
        self,
        *,
        token: str,
        host: str | None = None,
        discovery_timeout: float = 12.0,
        request_timeout: float = 8,
        display_names_store_path: Path | str | None = None,
    ) -> None:
        self._host_arg = host.strip() if host else None
        self._token = token
        self._request_timeout = request_timeout
        self._discovery_timeout = discovery_timeout
        self._display_names_store_path = (
            Path(display_names_store_path).expanduser().resolve() if display_names_store_path else None
        )
        self._host: str | None = None
        self._hub_mac: str | None = None
        self._tailwind: Tailwind | None = None
        self._alias_to_device: dict[str, GotailwindDevice] | None = None

    def __str__(self) -> str:
        if self._alias_to_device is None:
            return "GotailwindDeviceManager(not initialized)"
        unique = list({id(d): d for d in self._alias_to_device.values()}.values())
        unique.sort(key=lambda d: d.door_index)
        lines = [f"GotailwindDeviceManager(host={self._host}):"]
        for gd in unique:
            lines.append(f"  door {gd.door_index} ({gd.preferred_label!r}, id={gd.identifier!r}): {gd.door_state}")
        return "\n".join(lines)

    def _device_for(self, identifier: str) -> GotailwindDevice:
        if self._alias_to_device is None:
            raise NotInitializedError
        d = self._alias_to_device.get(identifier)
        if d is None:
            raise ValueError(f"Unknown door: {identifier!r}")
        return d

    async def _resolve_host(self) -> str:
        host = self._host_arg
        if not host:
            host = (os.environ.get("TAILWIND_HOST") or "").strip()
        if not host:
            host = await discover_tailwind_host(timeout=self._discovery_timeout)
        return host

    async def _tailwind_status(self, identifier: str) -> TailwindDoor:
        gd = self._device_for(identifier)
        assert self._tailwind is not None
        door = await self._tailwind.door_status(door=gd._door)
        gd._sync_reported_state(door.state)
        return door

    async def close(self, identifier: str) -> None:
        await self._device_for(identifier).close()

    async def disconnect(self) -> None:
        """Close the HTTP session; call ``fetch`` again to reuse the manager."""
        if self._tailwind is not None:
            await self._tailwind.close()
            self._tailwind = None
        self._alias_to_device = None
        self._host = None
        self._hub_mac = None

    @property
    def doors(self) -> tuple[GotailwindDevice, ...]:
        """Unique doors sorted by index (requires ``fetch`` first)."""
        if self._alias_to_device is None:
            raise NotInitializedError
        unique = list({id(d): d for d in self._alias_to_device.values()}.values())
        unique.sort(key=lambda d: d.door_index)
        return tuple(unique)

    def _expand_tailwind_lookup(self, uniq: list[GotailwindDevice]) -> dict[str, GotailwindDevice]:
        new_map: dict[str, GotailwindDevice] = {}
        for gd in uniq:
            new_map[gd.identifier] = gd
            if gd.door_key != gd.identifier:
                new_map[gd.door_key] = gd
            new_map[str(gd.door_index)] = gd
            pl = gd.preferred_label
            if pl not in (gd.identifier, str(gd.door_index), gd.door_key):
                new_map[pl] = gd
        return new_map

    def _finalize_tailwind_devices(self, uniq: list[GotailwindDevice]) -> None:
        if self._display_names_store_path is not None:
            for backend, key, disp in device_discovery_store.load_display_names(self._display_names_store_path):
                if backend != "tailwind":
                    continue
                for gd in uniq:
                    if key in {gd.identifier, gd.door_key}:
                        gd.set_display_name(disp)
                        break
        self._alias_to_device = self._expand_tailwind_lookup(uniq)

    def rebuild_lookup_after_display_change(self) -> None:
        """Rebuild lookup keys after changing display names on a managed door."""

        if self._alias_to_device is None:
            raise NotInitializedError
        uniq = list({id(gd): gd for gd in self._alias_to_device.values()}.values())
        self._alias_to_device = self._expand_tailwind_lookup(uniq)

    async def fetch(self) -> None:
        """Connect and enumerate doors. Each door is registered under its ``door_id`` and ``str(index)``."""
        if self._alias_to_device is not None:
            raise AlreadyInitializedError

        self._host = await self._resolve_host()
        cached_mac: str | None = None
        if self._display_names_store_path is not None:
            row = device_discovery_store.load_tailwind_host_row(self._display_names_store_path)
            if row is not None and row[0] == self._host:
                cached_mac = row[1]
        self._hub_mac = cached_mac or await asyncio.to_thread(lookup_mac_via_arp, self._host)
        if self._hub_mac is None:
            raise RuntimeError(
                f"Expected a MAC address for Tailwind hub at {self._host!r} "
                "(ARP/neighbor table miss); cannot admit doors without a hub MAC address"
            )
        if self._display_names_store_path is not None:
            device_discovery_store.save_tailwind_host(
                self._display_names_store_path,
                self._host,
                mac=self._hub_mac,
            )
        self._tailwind = Tailwind(
            host=self._host,
            token=self._token,
            request_timeout=self._request_timeout,
        )
        await self._tailwind.__aenter__()

        status = await self._tailwind.status()
        uniq: list[GotailwindDevice] = []
        for door_status in status.doors.values():
            door_id = door_status.door_id
            canonical = f"{self._hub_mac}:{door_id}"
            if self._display_names_store_path is not None and door_id != canonical:
                device_discovery_store.migrate_canonical_key_to_mac(
                    self._display_names_store_path,
                    backend="tailwind",
                    old_key=door_id,
                    mac=canonical,
                )
            uniq.append(
                GotailwindDevice(
                    identifier=canonical,
                    tailwind=self._tailwind,
                    door=door_status.index,
                    reported_state=door_status.state,
                    door_index=door_status.index,
                    door_key=door_id,
                    mac_address=self._hub_mac,
                )
            )
        self._finalize_tailwind_devices(uniq)

    def get_device_by_alias(self, identifier: str) -> GotailwindDevice | None:
        """Resolve a door by Tailwind ``door_id`` or by numeric index as a string (e.g. ``\"0\"``)."""
        if self._alias_to_device is None:
            raise NotInitializedError
        return self._alias_to_device.get(identifier)

    @property
    def host(self) -> str | None:
        """Host used for HTTP after the last successful ``fetch``; ``None`` if not connected."""
        return self._host

    async def is_closed(self, identifier: str) -> bool:
        door = await self._tailwind_status(identifier)
        return door.state == TailwindDoorState.CLOSED

    async def is_open(self, identifier: str) -> bool:
        door = await self._tailwind_status(identifier)
        return door.state == TailwindDoorState.OPEN

    async def open(self, identifier: str) -> None:
        await self._device_for(identifier).open()

    async def rediscover(self) -> None:
        """Clear the session and run :meth:`fetch` again (same host/env/mDNS rules as the first connect).

        Door IDs are re-read from the controller; use :meth:`disconnect` alone if you only need
        to drop connections without reloading.
        """

        await self.disconnect()
        await self.fetch()

    async def reload_from_cache(self, *, cache_path: Path | None = None) -> bool:
        """Reconnect to the SQLite-cached Tailwind host only (never mDNS).

        Uses ``cache_path`` or :attr:`_display_names_store_path` for
        ``load_tailwind_host``. On failure the prior session and door map are
        restored. Does not rewrite ``tailwind_last_host``.
        """

        path = cache_path or self._display_names_store_path
        if path is None:
            _LOGGER.debug("Tailwind reload_from_cache: no discovery cache path")
            return False
        if self._alias_to_device is None:
            _LOGGER.debug("Tailwind reload_from_cache: manager not initialized")
            return False
        host = (device_discovery_store.load_tailwind_host(path) or "").strip()
        if not host:
            _LOGGER.info("Tailwind reload_from_cache: empty cache; keeping prior device map")
            return False

        previous_host_arg = self._host_arg
        previous_tailwind = self._tailwind
        previous_map = self._alias_to_device
        previous_host = self._host

        self._host_arg = host
        self._tailwind = None
        self._alias_to_device = None
        self._host = None
        try:
            await self.fetch()
        except Exception:
            # ``self._tailwind`` was cleared before ``fetch``; pyright still types it
            # as None on this path even when ``fetch`` constructed a session then failed.
            failed_tailwind = cast(Tailwind | None, self._tailwind)
            if failed_tailwind is not None and failed_tailwind is not previous_tailwind:
                with contextlib.suppress(Exception):
                    await failed_tailwind.close()
            _LOGGER.warning(
                "Tailwind reload_from_cache: reconnect to %s failed; keeping prior device map",
                host,
                exc_info=True,
            )
            self._host_arg = previous_host_arg
            self._tailwind = previous_tailwind
            self._alias_to_device = previous_map
            self._host = previous_host
            return False

        self._host_arg = previous_host_arg
        if previous_tailwind is not None:
            with contextlib.suppress(Exception):
                await previous_tailwind.close()
        _LOGGER.info(
            "Tailwind reload_from_cache: replaced door map from cache host %s (%d door(s))",
            host,
            len(self.doors),
        )
        return True
