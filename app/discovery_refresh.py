"""Multi-family LAN rediscovery with new-device diff reporting.

Used by the CLI ``refresh-discovery`` command and Settings → Device Discovery.
Cache-first startup stays unchanged; this module only runs when an operator
(or API client) explicitly requests a full rediscover.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from app import device_discovery_store
from app.device_display import format_device_display
from app.device_manager import NotInitializedError

if TYPE_CHECKING:
    from app.domesti_bot_cli import DeviceManagersState

_LOGGER = logging.getLogger(__name__)

DISCOVERY_FAMILY_LABELS: dict[str, str] = {
    "androidtv": "Google Cast",
    "ep1": "Everything Presence One",
    "gotailwind": "GoTailwind",
    "kasa": "Kasa",
    "sonos": "Sonos",
    "vizio": "Vizio",
}

DISCOVERY_FAMILY_SLUGS: tuple[str, ...] = (
    "androidtv",
    "ep1",
    "gotailwind",
    "kasa",
    "sonos",
    "vizio",
)

FAMILY_SKIPPED_NOT_LOADED = "not loaded"

NEW_DEVICE_FOUND_PREFIX = "New device found:"


@dataclass(frozen=True, slots=True)
class DiscoveryDeviceSnapshot:
    """One device as seen before/after a rediscover sweep."""

    device_id: str
    display: str
    preferred_label: str


@dataclass(frozen=True, slots=True)
class DiscoveryFamilyResult:
    """Per-family outcome of :func:`refresh_all_device_discovery`."""

    device_count: int
    error: str | None
    family_id: str
    label: str
    new_devices: tuple[DiscoveryDeviceSnapshot, ...]
    ok: bool
    skipped: bool
    skip_detail: str | None
    source: str | None


@dataclass(frozen=True, slots=True)
class DiscoveryFamilyStatus:
    """Live roster status for Settings without running rediscover."""

    available: bool
    device_count: int
    family_id: str
    label: str
    last_discovery_source: str | None


@dataclass(frozen=True, slots=True)
class DiscoveryRefreshResult:
    """Aggregate rediscover outcome across all families."""

    families: tuple[DiscoveryFamilyResult, ...]
    new_devices: tuple[DiscoveryDeviceSnapshot, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class DiscoverySettingsStatus:
    """Settings GET payload: cache-first explanation is owned by the UI copy."""

    families: tuple[DiscoveryFamilyStatus, ...]


def discovery_settings_status(state: DeviceManagersState) -> DiscoverySettingsStatus:
    """Snapshot per-family availability, counts, and last discovery source."""
    families: list[DiscoveryFamilyStatus] = []
    for slug in DISCOVERY_FAMILY_SLUGS:
        mgr = _manager_for_slug(state, slug)
        label = DISCOVERY_FAMILY_LABELS[slug]
        if mgr is None:
            families.append(
                DiscoveryFamilyStatus(
                    available=False,
                    device_count=0,
                    family_id=slug,
                    label=label,
                    last_discovery_source=None,
                )
            )
            continue
        snapshot = snapshot_family_devices(state, slug)
        families.append(
            DiscoveryFamilyStatus(
                available=True,
                device_count=len(snapshot),
                family_id=slug,
                label=label,
                last_discovery_source=_last_discovery_source(mgr),
            )
        )
    return DiscoverySettingsStatus(families=tuple(families))


async def refresh_all_device_discovery(
    state: DeviceManagersState,
    *,
    restart_watchers: bool = True,
) -> DiscoveryRefreshResult:
    """Run LAN rediscover for every loaded family and report newly admitted devices.

    Snapshots device ids before rediscover, gathers per-family ``rediscover()``
    calls in parallel (same order as the CLI), then diffs after. When
    ``restart_watchers`` is true and at least one family rediscovered
    successfully, restarts device-state watchers once.
    """
    before_by_family = {slug: snapshot_family_devices(state, slug) for slug in DISCOVERY_FAMILY_SLUGS}

    bundles = await asyncio.gather(*(_rediscover_family(state, slug) for slug in DISCOVERY_FAMILY_SLUGS))
    bundle_by_slug = {b["slug"]: b for b in bundles}

    any_ok = False
    family_results: list[DiscoveryFamilyResult] = []
    flat_new: list[DiscoveryDeviceSnapshot] = []
    for slug in DISCOVERY_FAMILY_SLUGS:
        bundle = bundle_by_slug[slug]
        label = DISCOVERY_FAMILY_LABELS[slug]
        if bundle["skipped"]:
            family_results.append(
                DiscoveryFamilyResult(
                    device_count=0,
                    error=None,
                    family_id=slug,
                    label=label,
                    new_devices=(),
                    ok=False,
                    skipped=True,
                    skip_detail=str(bundle.get("detail") or FAMILY_SKIPPED_NOT_LOADED),
                    source=None,
                )
            )
            continue
        if not bundle["ok"]:
            exc = bundle.get("exc")
            family_results.append(
                DiscoveryFamilyResult(
                    device_count=len(before_by_family[slug]),
                    error=repr(exc) if exc is not None else "rediscover failed",
                    family_id=slug,
                    label=label,
                    new_devices=(),
                    ok=False,
                    skipped=False,
                    skip_detail=None,
                    source=None,
                )
            )
            continue

        any_ok = True
        after = snapshot_family_devices(state, slug)
        before = before_by_family[slug]
        new_ids = sorted(set(after) - set(before))
        new_devices = tuple(after[device_id] for device_id in new_ids)
        flat_new.extend(new_devices)
        mgr = _manager_for_slug(state, slug)
        family_results.append(
            DiscoveryFamilyResult(
                device_count=len(after),
                error=None,
                family_id=slug,
                label=label,
                new_devices=new_devices,
                ok=True,
                skipped=False,
                skip_detail=None,
                source=_last_discovery_source(mgr) if mgr is not None else None,
            )
        )

    if restart_watchers and any_ok:
        from app.server_runtime import runtime

        if runtime.device_state is not None:
            try:
                await runtime.restart_device_state_watchers()
            except Exception:
                _LOGGER.warning(
                    "Device state watcher restart after rediscover failed",
                    exc_info=True,
                )

    return DiscoveryRefreshResult(
        families=tuple(family_results),
        new_devices=tuple(flat_new),
    )


def snapshot_family_devices(
    state: DeviceManagersState,
    family_id: str,
) -> dict[str, DiscoveryDeviceSnapshot]:
    """Return ``device_id → snapshot`` for a family, or ``{}`` if unloaded/uninitialized."""
    collector = _FAMILY_COLLECTORS.get(family_id)
    if collector is None:
        return {}
    mgr = _manager_for_slug(state, family_id)
    if mgr is None:
        return {}
    try:
        return collector(mgr)
    except NotInitializedError:
        return {}


def _collect_androidtv(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.switches)


def _collect_ep1(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.devices)


def _collect_gotailwind(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.doors)


def _collect_kasa(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.switches)


def _collect_sonos(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.players)


def _collect_vizio(mgr: Any) -> dict[str, DiscoveryDeviceSnapshot]:
    return _snapshots_from_devices(mgr.tvs)


_FAMILY_COLLECTORS: Mapping[str, Callable[[Any], dict[str, DiscoveryDeviceSnapshot]]] = {
    "androidtv": _collect_androidtv,
    "ep1": _collect_ep1,
    "gotailwind": _collect_gotailwind,
    "kasa": _collect_kasa,
    "sonos": _collect_sonos,
    "vizio": _collect_vizio,
}


def _last_discovery_source(mgr: Any) -> str | None:
    return getattr(mgr, "last_discovery_source", None)


def _manager_for_slug(state: DeviceManagersState, slug: str) -> Any | None:
    if slug == "androidtv":
        return state.androidtv_mgr
    if slug == "ep1":
        return state.ep1_mgr
    if slug == "gotailwind":
        return state.tailwind_mgr
    if slug == "kasa":
        return state.kasa_mgr
    if slug == "sonos":
        return state.sonos_mgr
    if slug == "vizio":
        return state.vizio_mgr
    return None


async def _rediscover_family(state: DeviceManagersState, slug: str) -> dict[str, Any]:
    mgr = _manager_for_slug(state, slug)
    if mgr is None:
        return {
            "slug": slug,
            "skipped": True,
            "detail": FAMILY_SKIPPED_NOT_LOADED,
            "exc": None,
            "ok": False,
        }
    try:
        await mgr.rediscover()
        if slug == "gotailwind" and state.cache_path is not None:
            host = getattr(mgr, "host", None)
            if host:
                device_discovery_store.save_tailwind_host(state.cache_path, host)
        return {
            "slug": slug,
            "skipped": False,
            "detail": "",
            "exc": None,
            "ok": True,
        }
    except Exception as ex:
        return {
            "slug": slug,
            "skipped": False,
            "detail": "",
            "exc": ex,
            "ok": False,
        }


def _snapshots_from_devices(devices: Sequence[Any]) -> dict[str, DiscoveryDeviceSnapshot]:
    out: dict[str, DiscoveryDeviceSnapshot] = {}
    for device in devices:
        device_id = str(device.identifier)
        preferred = str(device.preferred_label)
        out[device_id] = DiscoveryDeviceSnapshot(
            device_id=device_id,
            display=format_device_display(device_id, preferred),
            preferred_label=preferred,
        )
    return out
