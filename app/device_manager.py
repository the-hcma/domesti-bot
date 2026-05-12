"""Shared device-manager protocol and errors."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

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
    """Lifecycle shared by all backends: discover devices and optionally tear down sessions."""

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

    @abstractmethod
    async def is_closed(self, identifier: str) -> bool:
        """``True`` when the door reports fully closed."""

    @abstractmethod
    async def is_open(self, identifier: str) -> bool:
        """``True`` when the door reports fully open."""

    @abstractmethod
    async def open(self, identifier: str) -> None:
        """Fully open the door (or equivalent)."""


class SpeakerDeviceManager(DeviceManager[SpeakerT], ABC):
    """Speakers / zones: *pause* / *resume* playback."""

    @abstractmethod
    async def pause(self, identifier: str) -> None:
        """Pause playback on the resolved zone."""

    @abstractmethod
    async def resume(self, identifier: str) -> None:
        """Resume playback on the resolved zone."""


class SwitchDeviceManager(DeviceManager[SwitchT], ABC):
    """Plugs, bulbs, relays: *off* / *on* power semantics."""

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
