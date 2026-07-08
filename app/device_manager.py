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

    async def flip(self, identifier: str) -> str:
        """Flip from cached door state; return ``[ui-action]`` detail."""
        return await self._flip_device(identifier)

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
    async def _flip_device(self, identifier: str) -> str:
        """Resolve *identifier* and call :meth:`DoorDevice.flip`."""


class SpeakerDeviceManager(DeviceManager[SpeakerT], ABC):
    """Speakers / zones: *pause* / *resume* playback."""

    async def flip(
        self,
        identifier: str,
        *,
        favorite_index: int = 0,
    ) -> str:
        """Flip from cached playback state; return ``[ui-action]`` detail."""
        return await self._flip_device(identifier, favorite_index=favorite_index)

    @abstractmethod
    async def pause(self, identifier: str) -> None:
        """Pause playback on the resolved zone."""

    @abstractmethod
    async def resume(self, identifier: str, *, favorite_index: int = 0) -> None:
        """Resume playback on the resolved zone."""

    @abstractmethod
    async def _flip_device(
        self,
        identifier: str,
        *,
        favorite_index: int = 0,
    ) -> str:
        """Resolve *identifier* and call :meth:`SpeakerDevice.flip`."""


class SwitchDeviceManager(DeviceManager[SwitchT], ABC):
    """Plugs, bulbs, relays: *off* / *on* power semantics."""

    async def flip(self, identifier: str) -> str:
        """Flip from cached on/off; return ``[ui-action]`` detail."""
        return await self._flip_device(identifier)

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
    async def _flip_device(self, identifier: str) -> str:
        """Resolve *identifier* and call :meth:`SwitchDevice.flip`."""
