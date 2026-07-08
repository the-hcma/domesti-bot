"""Shared device-manager protocol and errors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Generic, Self, TypeVar

from app.rule_engine import Device, DoorDevice, SpeakerDevice, SwitchDevice


class AlreadyInitializedError(Exception):
    """Raised when ``fetch`` is called again without resetting the manager."""

    pass


class NotInitializedError(Exception):
    """Raised when resolving a device before ``fetch`` has succeeded."""

    pass


D = TypeVar("D", bound=Device)
DoorT = TypeVar("DoorT", bound=DoorDevice)
SpeakerT = TypeVar("SpeakerT", bound=SpeakerDevice)
SwitchT = TypeVar("SwitchT", bound=SwitchDevice)


class DeviceManager(ABC, Generic[D]):
    """Lifecycle shared by all backends: discover devices and optionally tear down sessions.

    Supports use as an async context manager so callers can write
    ``async with KasaDeviceManager(...) as mgr: await mgr.fetch(); ...``
    instead of an explicit ``try / finally: await mgr.disconnect()``. The
    ``__aenter__`` does not call ``fetch()`` — entry is cheap; callers
    still drive discovery explicitly so they control timing and arguments.
    """

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.disconnect()

    async def disconnect(self) -> None:
        """Release network resources; override when the backing client needs cleanup."""
        return None

    @abstractmethod
    async def fetch(self) -> None:
        """Discover or connect and populate the alias → device map."""


class DoorDeviceManager(DeviceManager[DoorT], ABC):
    """Garage-style hardware: *open* / *closed*, not power on/off."""

    @abstractmethod
    async def close(self, identifier: str) -> None:
        """Fully close the door (or equivalent)."""

    def device_label(self, identifier: str) -> str:
        """Return the display label for *identifier* (raises on unknown id)."""
        return self._device_for(identifier).preferred_label

    async def flip(self, identifier: str) -> str:
        """Flip from cached door state; return ``[ui-action]`` detail."""
        _label, detail = await self.flip_tile(identifier)
        return detail

    async def flip_tile(self, identifier: str) -> tuple[str, str]:
        """Flip one door with a single lookup; return ``(label, log_detail)``."""
        try:
            device = self._device_for(identifier)
        except ValueError as exc:
            raise KeyError(identifier) from exc
        detail = await device.flip()
        return device.preferred_label, detail

    @abstractmethod
    async def is_closed(self, identifier: str) -> bool:
        """``True`` when the door reports fully closed."""

    @abstractmethod
    async def is_open(self, identifier: str) -> bool:
        """``True`` when the door reports fully open."""

    @abstractmethod
    async def open(self, identifier: str) -> None:
        """Fully open the door (or equivalent)."""

    @abstractmethod
    def _device_for(self, identifier: str) -> DoorT:
        """Resolve *identifier* to a live door device."""


class SpeakerDeviceManager(DeviceManager[SpeakerT], ABC):
    """Speakers / zones: *pause* / *resume* playback."""

    def device_label(self, identifier: str) -> str:
        """Return the display label for *identifier* (raises on unknown id)."""
        return self._device_for(identifier).preferred_label

    async def flip(
        self,
        identifier: str,
        *,
        favorite_index: int = 0,
    ) -> str:
        """Flip from cached playback state; return ``[ui-action]`` detail."""
        _label, detail = await self.flip_tile(
            identifier,
            favorite_index=favorite_index,
        )
        return detail

    async def flip_tile(
        self,
        identifier: str,
        *,
        favorite_index: int = 0,
    ) -> tuple[str, str]:
        """Flip one zone with a single lookup; return ``(label, log_detail)``."""
        try:
            device = self._device_for(identifier)
        except ValueError as exc:
            raise KeyError(identifier) from exc
        detail = await device.flip(favorite_index=favorite_index)
        return device.preferred_label, detail

    @abstractmethod
    async def pause(self, identifier: str) -> None:
        """Pause playback on the resolved zone."""

    @abstractmethod
    async def resume(self, identifier: str, *, favorite_index: int = 0) -> None:
        """Resume playback on the resolved zone."""

    @abstractmethod
    def _device_for(self, identifier: str) -> SpeakerT:
        """Resolve *identifier* to a live speaker device."""


class SwitchDeviceManager(DeviceManager[SwitchT], ABC):
    """Plugs, bulbs, relays: *off* / *on* power semantics."""

    def device_label(self, identifier: str) -> str:
        """Return the display label for *identifier* (raises on unknown id)."""
        return self._device_for(identifier).preferred_label

    async def flip(self, identifier: str) -> str:
        """Flip from cached on/off; return ``[ui-action]`` detail."""
        _label, detail = await self.flip_tile(identifier)
        return detail

    async def flip_tile(self, identifier: str) -> tuple[str, str]:
        """Flip one switch with a single lookup; return ``(label, log_detail)``."""
        try:
            device = self._device_for(identifier)
        except ValueError as exc:
            raise KeyError(identifier) from exc
        detail = await device.flip()
        return device.preferred_label, detail

    @abstractmethod
    async def is_off(self, identifier: str) -> bool:
        """``True`` when the switch reports off / no power."""

    @abstractmethod
    async def is_on(self, identifier: str) -> bool:
        """``True`` when the switch reports on / powered."""

    @abstractmethod
    async def turn_off(self, identifier: str) -> None:
        """Turn power off."""

    @abstractmethod
    async def turn_on(self, identifier: str) -> None:
        """Turn power on."""

    @abstractmethod
    def _device_for(self, identifier: str) -> SwitchT:
        """Resolve *identifier* to a live switch device."""
