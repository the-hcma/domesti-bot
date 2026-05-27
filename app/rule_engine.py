from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from enum import StrEnum
from math import sqrt
from typing import Any, Generic, TypeVar
from pyproj import Transformer

# Transform to UTM (a standard meters-based coordinate system)

UTM_TRANSFORMER = Transformer.from_crs("EPSG:4326", "EPSG:32618")  # WGS84 → UTM zone 18N


class DoorPosition(StrEnum):
    """Fully-open vs fully-closed door (cached view)."""

    CLOSED = "closed"
    OPEN = "open"


class SwitchPowerState(StrEnum):
    """Relay / outlet power view."""

    OFF = "off"
    ON = "on"


class Device:
    """Location-aware participant (phones, tags, etc.). Has no switch or door commands."""

    __slots__ = ("_identifier", "_display_name", "_lat", "_lon", "_x", "_y")

    def __init__(
        self, identifier: str, *, display_name: str | None = None
    ) -> None:
        self._identifier = identifier
        self._display_name = (
            display_name.strip() if display_name and display_name.strip() else None
        )
        self._lat, self._lon = None, None
        self._x, self._y = None, None

    def __str__(self) -> str:
        label = self.preferred_label
        return f"{label} @ ({self._lat}, {self._lon})"

    @property
    def identifier(self) -> str:
        return self._identifier

    @property
    def display_name(self) -> str | None:
        """Optional human-facing label; when set, :meth:`preferred_label` uses it."""

        return self._display_name

    @property
    def preferred_label(self) -> str:
        """Label for UI / CLI (display name when present, else ``identifier``)."""

        return self._display_name if self._display_name else self._identifier

    def set_display_name(self, value: str | None) -> None:
        """Set or clear the optional display name (whitespace-only clears)."""

        self._display_name = (
            value.strip() if value and value.strip() else None
        )

    @property
    def lat(self):
        return self._lat

    @property
    def lon(self):
        return self._lon

    def setLocation(self, lat: float, lon: float) -> None:
        self._lat = lat
        self._lon = lon
        self._x, self._y = UTM_TRANSFORMER.transform(self._lat, self._lon)

    @property
    def x(self):
        return self._x

    @property
    def y(self):
        return self._y


class DoorDevice(Device, ABC):
    """Garage-style actuator: *open* / *closed* only — no ``turn_on`` / ``turn_off``."""

    __slots__ = ()

    def __init__(
        self, identifier: str, *, display_name: str | None = None
    ) -> None:
        super().__init__(identifier, display_name=display_name)

    @abstractmethod
    async def close(self) -> None:
        """Fully close."""

    @property
    def door_state(self) -> DoorPosition:
        """Cached fully-open vs fully-closed."""
        return DoorPosition.OPEN if self.is_open else DoorPosition.CLOSED

    @property
    @abstractmethod
    def is_closed(self) -> bool:
        """Fully closed."""

    @property
    @abstractmethod
    def is_open(self) -> bool:
        """Fully open."""

    @abstractmethod
    async def open(self) -> None:
        """Fully open."""


class SpeakerDevice(Device, ABC):
    """Network speaker / zone: *pause* / *resume* playback — not switches or doors."""

    __slots__ = ()

    def __init__(
        self, identifier: str, *, display_name: str | None = None
    ) -> None:
        super().__init__(identifier, display_name=display_name)

    @abstractmethod
    async def pause(self) -> None:
        """Pause whatever is currently playing."""

    @abstractmethod
    async def resume(self, *, favorite_index: int = 0) -> None:
        """Resume playback (optionally from a configured stream favorite)."""


class SwitchDevice(Device, ABC):
    """Relay / bulb / plug: *off* / *on* only — no ``open`` / ``close``."""

    __slots__ = ("_on",)

    def __init__(
        self, identifier: str, *, display_name: str | None = None
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._on = False

    @property
    def is_off(self) -> bool:
        return not self._on

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def power_state(self) -> SwitchPowerState:
        """Cached on/off."""
        return SwitchPowerState.ON if self._on else SwitchPowerState.OFF

    def set_power(self, on: bool) -> None:
        """Update cached on/off (hardware subclasses refresh after I/O)."""
        self._on = on

    @abstractmethod
    async def turn_off(self) -> None:
        """Turn power off."""

    @abstractmethod
    async def turn_on(self) -> None:
        """Turn power on."""


class SimulatedSwitchDevice(SwitchDevice):
    """In-memory switch for tests and simulations."""

    __slots__ = ()

    async def turn_off(self) -> None:
        self.set_power(False)

    async def turn_on(self) -> None:
        self.set_power(True)


# Sync / async callables taking a single ``Device`` (or a subclass).
# Parameterized on the concrete device type so call sites like
# ``AsyncCallableAction(SimulatedSwitchDevice(...), SimulatedSwitchDevice.turn_on)``
# type-check without a cast — pyright infers ``D = SimulatedSwitchDevice``
# from the first argument and the effect signature is consistent with it.
D = TypeVar("D", bound=Device)
DeviceEffect = Callable[[D], Any]
AsyncDeviceEffect = Callable[[D], Coroutine[Any, Any, Any]]


class Action(ABC, Generic[D]):
    """Common binding to a ``Device``. Subclasses define sync or async ``run``."""

    def __init__(self, device: D) -> None:
        self._device = device

    @property
    def device(self) -> D:
        return self._device


class CallableAction(Action[D]):
    """Synchronous ``(device) ->`` effect invoked by ``run()``."""

    def __init__(self, device: D, effect: DeviceEffect[D]) -> None:
        super().__init__(device)
        self._effect = effect

    def run(self) -> Any:
        return self._effect(self._device)


class AsyncCallableAction(Action[D]):
    """Async ``(device) ->`` coroutine awaited by ``run()``."""

    def __init__(self, device: D, effect: AsyncDeviceEffect[D]) -> None:
        super().__init__(device)
        self._effect = effect

    async def run(self) -> Any:
        return await self._effect(self._device)


class Condition:
    """Predicate invoked when ``is_true`` runs (late evaluation)."""

    def __init__(self, predicate: Callable[..., bool]) -> None:
        self._predicate = predicate

    def is_true(self, *args: Any, **kwargs: Any) -> bool:
        return bool(self._predicate(*args, **kwargs))


class Geofence:

    def __init__(self, description: str, lat: float, lon: float, radius: float) -> None:
        self._description = description
        self._lat = lat
        self._lon = lon
        self._radius = radius
        self._x, self._y = UTM_TRANSFORMER.transform(self._lat, self._lon)  # lat, lon

    def __str__(self) -> str:
        return f"{self._description} ({self._lat}, {self._lon}, {self._radius})"

    def _is_inside(self, device: Device) -> bool:
        distance_from_center = sqrt((device.x - self._x) ** 2 + (device.y - self._y) ** 2)
        return distance_from_center <= self._radius

    def is_inside(self, devices: set[Device]) -> bool:
        result = True
        for device in devices:
            result = result and self._is_inside(device)
        return result


class Rule:
    pass


class RuleEngine:
    pass
