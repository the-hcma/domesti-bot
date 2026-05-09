"""Sonos zone control via SoCo (UPnP over the LAN).

Compatible with **S1-era and newer** households that expose the classic Sonos SOAP API on the
local network (the same stack SoCo targets). Discovery uses UDP; playback calls are run in a
thread pool so async callers are not blocked.

Requires the optional ``soco`` dependency (see ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from soco import discover as soco_discover

from device_manager import AlreadyInitializedError, NotInitializedError, SpeakerDeviceManager
from rule_engine import SpeakerDevice

_SONOS_TRANSPORT_LABELS: dict[str, str] = {
    "PAUSED_PLAYBACK": "paused",
    "PLAYING": "playing",
    "STOPPED": "stopped",
    "TRANSITIONING": "transitioning",
}


class SonosSpeakerDevice(SpeakerDevice):
    __slots__ = ("_soco",)

    def __init__(
        self,
        identifier: str,
        soco_zone: Any,
        *,
        display_name: str | None = None,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._soco = soco_zone

    async def pause(self) -> None:
        await asyncio.to_thread(self._soco.pause)

    async def resume(self) -> None:
        await asyncio.to_thread(self._soco.play)

    def transport_state_summary(self) -> str:
        """Best-effort playback view from UPnP AV transport (``playing`` / ``paused`` / …)."""

        try:
            info = self._soco.get_current_transport_info()
            raw = (info or {}).get("current_transport_state") or ""
            raw = str(raw).strip()
        except Exception:
            return "unknown"
        if not raw:
            return "unknown"
        return _SONOS_TRANSPORT_LABELS.get(
            raw, raw.replace("_", " ").lower()
        )


class SonosDeviceManager(SpeakerDeviceManager[SonosSpeakerDevice]):
    """Discover zones with SoCo and drive *pause* / *resume* per zone."""

    def __init__(self, *, discovery_timeout: float = 5.0) -> None:
        self._discovery_timeout = discovery_timeout
        self._alias_to_device: dict[str, SonosSpeakerDevice] | None = None

    def _device_for(self, identifier: str) -> SonosSpeakerDevice:
        if self._alias_to_device is None:
            raise NotInitializedError
        d = self._alias_to_device.get(identifier)
        if d is None:
            raise ValueError(f"Unknown Sonos zone: {identifier!r}")
        return d

    def _expand_lookup(self, devices: list[SonosSpeakerDevice]) -> dict[str, SonosSpeakerDevice]:
        alias_map: dict[str, SonosSpeakerDevice] = {}
        for sd in devices:
            alias_map[sd.identifier] = sd
            label = sd.preferred_label
            if label != sd.identifier:
                alias_map[label] = sd
        return alias_map

    async def disconnect(self) -> None:
        self._alias_to_device = None

    async def fetch(self) -> None:
        if self._alias_to_device is not None:
            raise AlreadyInitializedError

        timeout = self._discovery_timeout

        def _run_discovery() -> set[Any]:
            found = soco_discover(timeout=timeout)
            return found if found else set()

        zones = await asyncio.to_thread(_run_discovery)
        devices: list[SonosSpeakerDevice] = []
        for z in zones:
            uid = getattr(z, "uid", None) or str(id(z))
            name = (getattr(z, "player_name", None) or "").strip() or uid
            sd = SonosSpeakerDevice(uid, z, display_name=name)
            devices.append(sd)
        devices.sort(key=lambda d: d.preferred_label.lower())
        self._alias_to_device = self._expand_lookup(devices)

    def get_device_by_alias(self, identifier: str) -> SonosSpeakerDevice | None:
        if self._alias_to_device is None:
            raise NotInitializedError
        return self._alias_to_device.get(identifier)

    async def pause(self, identifier: str) -> None:
        await self._device_for(identifier).pause()

    @property
    def players(self) -> tuple[SonosSpeakerDevice, ...]:
        if self._alias_to_device is None:
            raise NotInitializedError
        uniq = list({id(p): p for p in self._alias_to_device.values()}.values())
        uniq.sort(key=lambda p: p.preferred_label.lower())
        return tuple(uniq)

    async def rediscover(self) -> None:
        await self.disconnect()
        await self.fetch()

    async def resume(self, identifier: str) -> None:
        await self._device_for(identifier).resume()
