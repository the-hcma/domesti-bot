"""Canonical home coordinates for distance, astronomy, and vacation mode.

``settings_location.lat`` / ``lon`` in the automation rules bundle are the house
point. Distance-based features (vacation mode, ``users_min_distance_from_home_m``)
must call :func:`resolve_home_location` rather than reading geofence centers.

Unconfigured sentinel: both ``lat`` and ``lon`` exactly ``0.0`` (matches the web
UI map default). Consumers that need home must treat that as missing and fail
closed — do not pretend Gulf of Guinea is the house.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.api.schemas import SettingsLocationOut


class HomeLocationNotConfiguredError(ValueError):
    """Raised when settings_location has no usable home coordinates."""


@dataclass(frozen=True, slots=True)
class HomeLocationRef:
    """Resolved home point from ``settings_location``."""

    home_label: str | None
    lat: float
    lon: float
    timezone: str


def home_coordinates_configured(*, lat: float, lon: float) -> bool:
    """Return True when ``lat``/``lon`` are not the unconfigured ``0.0``/``0.0`` sentinel."""
    return not (lat == 0.0 and lon == 0.0)


def home_location_configured(settings: SettingsLocationOut) -> bool:
    """Return True when ``settings`` has a usable home lat/lon (not the 0,0 sentinel)."""
    return home_coordinates_configured(lat=settings.lat, lon=settings.lon)


def resolve_home_location(settings: SettingsLocationOut) -> HomeLocationRef:
    """Return the configured home point, or raise if unconfigured / invalid."""
    if not home_location_configured(settings):
        raise HomeLocationNotConfiguredError(
            "Expected settings_location lat/lon to be configured home coordinates, "
            "got lat=0.0 lon=0.0 (unconfigured sentinel)"
        )
    if not (-90.0 <= settings.lat <= 90.0):
        raise HomeLocationNotConfiguredError(
            f"Expected settings_location lat in [-90, 90], got {settings.lat}"
        )
    if not (-180.0 <= settings.lon <= 180.0):
        raise HomeLocationNotConfiguredError(
            f"Expected settings_location lon in [-180, 180], got {settings.lon}"
        )
    label = settings.home_label
    if label is not None:
        trimmed = label.strip()
        label = trimmed if trimmed else None
    return HomeLocationRef(
        home_label=label,
        lat=settings.lat,
        lon=settings.lon,
        timezone=settings.timezone,
    )


def try_resolve_home_location(settings: SettingsLocationOut) -> HomeLocationRef | None:
    """Return home when configured, else ``None`` (fail closed without raising)."""
    try:
        return resolve_home_location(settings)
    except HomeLocationNotConfiguredError:
        return None
