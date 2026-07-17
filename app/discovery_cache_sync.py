"""Sync in-memory device managers from the shared discovery SQLite cache.

The CLI ``refresh-discovery`` path writes ``device_discovery.sqlite`` while the
HTTP process keeps live ``*DeviceManager`` maps. Callers
(``GET /v1/ui/state``) invoke :func:`maybe_sync_discovery_cache` so the UI
roster tracks cache drift without LAN rediscovery (UDP / mDNS / SSDP).

Drift fingerprints use **stable identity only** — normalized MAC addresses
(Kasa, Tailwind hub, Vizio) or vendor UUIDs (Sonos RINCON, Cast). IP
addresses are never part of the fingerprint: they drift with DHCP and must
not decide roster membership. A host-only change in the cache therefore
does not trigger a resync; adding / removing a stable identity does.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import device_discovery_store
from app.device_enums import DeviceFamilyId
from app.device_mac import try_normalize_mac
from app.device_manager import NotInitializedError
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_credentials import resolve_vizio_auth_token, vizio_device_id_from_parts
from app.vizio_device_manager import VizioDeviceManager

_LOGGER = logging.getLogger(__name__)


async def maybe_sync_discovery_cache(state: DeviceManagersState) -> bool:
    """Reload every ready family from SQLite when its cached roster drifts.

    Returns ``True`` when at least one in-memory map was replaced and state
    watchers were restarted. Never runs LAN discovery broadcasts. Per-family
    failures are logged and do not abort the remaining families or skip
    watcher restart for successful replacements.
    """

    cache_path = state.cache_path
    if cache_path is None:
        return False
    if not _any_family_needs_sync(state, cache_path):
        return False

    async with runtime.discovery_cache_sync_lock:
        any_ok = False
        for sync_fn in (
            _sync_androidtv,
            _sync_kasa,
            _sync_sonos,
            _sync_tailwind,
            _sync_vizio,
        ):
            try:
                if await sync_fn(state, cache_path):
                    any_ok = True
            except Exception:
                _LOGGER.exception(
                    "discovery cache sync failed during %s; continuing other families",
                    sync_fn.__name__,
                )
        if any_ok:
            await runtime.restart_device_state_watchers()
        return any_ok


async def maybe_sync_kasa_from_discovery_cache(state: DeviceManagersState) -> bool:
    """Backward-compatible alias for :func:`maybe_sync_discovery_cache`."""

    return await maybe_sync_discovery_cache(state)


def _androidtv_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.androidtv_mgr
    if mgr is None:
        return False
    # Empty cache and incomplete-UUID rows both yield frozenset() — keep prior map.
    cached = _cached_androidtv_uuids(cache_path)
    if not cached:
        return False
    try:
        live = _live_androidtv_uuids(mgr)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.ANDROIDTV)


def _any_family_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    return (
        _androidtv_needs_sync(state, cache_path)
        or _kasa_needs_sync(state, cache_path)
        or _sonos_needs_sync(state, cache_path)
        or _tailwind_needs_sync(state, cache_path)
        or _vizio_needs_sync(state, cache_path)
    )


def _cached_androidtv_uuids(cache_path: Path) -> frozenset[str]:
    known = device_discovery_store.load_androidtv_known_devices(cache_path)
    if not known or not all(uid for _h, _p, _fn, uid, _model, _mac in known):
        return frozenset()
    return frozenset(uid for _h, _p, _fn, uid, _model, _mac in known if uid)


def _cached_kasa_macs(cache_path: Path) -> frozenset[str]:
    """Normalized MACs from cache rows; MAC-less rows are excluded from the fingerprint."""

    macs: set[str] = set()
    for _host, _alias, _cfg, _requires_klap, mac in device_discovery_store.load_cached_configs(cache_path):
        normalized = try_normalize_mac(mac or "")
        if normalized:
            macs.add(normalized)
    return frozenset(macs)


def _cached_sonos_uids(cache_path: Path) -> frozenset[str]:
    return frozenset(
        uid.strip().upper()
        for uid, _host, _name, _mac in device_discovery_store.load_sonos_zones(cache_path)
        if uid.strip()
    )


def _cached_tailwind_hub_macs(cache_path: Path) -> frozenset[str]:
    row = device_discovery_store.load_tailwind_host_row(cache_path)
    if row is None:
        return frozenset()
    mac = try_normalize_mac(row[1] or "")
    return frozenset({mac} if mac else ())


def _cached_vizio_ids(cache_path: Path, mgr: VizioDeviceManager) -> frozenset[str]:
    """IDs that ``reload_from_cache`` would attempt (token-backed endpoints only)."""

    ids: set[str] = set()
    for host, port, _display, _model, mac, _diid in device_discovery_store.load_vizio_tvs(cache_path):
        token, _source = resolve_vizio_auth_token(
            mac=mac,
            host=host,
            cli_token=mgr._cli_auth_token,
            env_token=mgr._env_auth_token,
            cache_path=cache_path,
        )
        if not token:
            continue
        ids.add(vizio_device_id_from_parts(mac=mac, host=host, port=port))
    return frozenset(ids)


def _clear_failed(family: DeviceFamilyId) -> None:
    runtime.discovery_cache_sync_failed.pop(family.value, None)


def _failed_fp(family: DeviceFamilyId) -> frozenset[str] | None:
    return runtime.discovery_cache_sync_failed.get(family.value)


def _kasa_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    cached = _cached_kasa_macs(cache_path)
    if not cached:
        return False
    try:
        live = _live_kasa_macs(state.kasa_mgr, cache_path)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.KASA)


def _live_androidtv_uuids(mgr: object) -> frozenset[str]:
    switches = getattr(mgr, "switches")
    return frozenset(str(dev.identifier).strip() for dev in switches if str(dev.identifier).strip())


def _live_kasa_macs(mgr: KasaDeviceManager, cache_path: Path) -> frozenset[str]:
    """Normalized MACs the manager accounts for (connected or KLAP-skipped).

    Skipped KLAP-auth hosts have no live device object, so their MACs come
    from the matching cache rows. Endpoints without a known MAC are excluded
    on both sides — a DHCP address change alone must never register as drift.
    """

    macs = {kd.mac_address for kd in mgr.switches}
    macs.discard("")
    skipped = {(host or "").strip() for host in mgr.skipped_auth_hosts}
    skipped.discard("")
    if skipped:
        for host, _alias, _cfg, _requires_klap, mac in device_discovery_store.load_cached_configs(cache_path):
            if host.strip() not in skipped:
                continue
            normalized = try_normalize_mac(mac or "")
            if normalized:
                macs.add(normalized)
    return frozenset(macs)


def _live_sonos_uids(mgr: SonosDeviceManager) -> frozenset[str]:
    """Fingerprint live zones by RINCON uid, matching ``sonos_known_zones.uuid``.

    ``SonosSpeakerDevice.identifier`` is the MAC (primary UI id) and would
    never match the cache; the zone's host is excluded so a DHCP move alone
    never looks like roster drift. UIDs are uppercased so mixed-case cache
    rows cannot flap against SoCo's usual uppercase ``.uid``.
    """

    return frozenset(uid.strip().upper() for sd in mgr.players if (uid := (sd.rincon_uid or "")).strip())


def _live_tailwind_hub_macs(mgr: GotailwindDeviceManager) -> frozenset[str]:
    mac = try_normalize_mac(mgr.hub_mac or "")
    return frozenset({mac} if mac else ())


def _live_vizio_ids(mgr: VizioDeviceManager) -> frozenset[str]:
    return frozenset(tv.identifier for tv in mgr.tvs)


def _mark_failed(family: DeviceFamilyId, fingerprint: frozenset[str]) -> None:
    runtime.discovery_cache_sync_failed[family.value] = fingerprint


def _sonos_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.sonos_mgr
    if mgr is None:
        return False
    cached = _cached_sonos_uids(cache_path)
    if not cached:
        return False
    try:
        live = _live_sonos_uids(mgr)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.SONOS)


async def _sync_androidtv(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.androidtv_mgr
    if mgr is None or not _androidtv_needs_sync(state, cache_path):
        return False
    cached = _cached_androidtv_uuids(cache_path)
    _LOGGER.info(
        "AndroidTV discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_androidtv_uuids(mgr)),
        sorted(cached),
    )
    ok = await mgr.reload_from_cache()
    if not ok:
        _mark_failed(DeviceFamilyId.ANDROIDTV, cached)
        return False
    _clear_failed(DeviceFamilyId.ANDROIDTV)
    return True


async def _sync_kasa(state: DeviceManagersState, cache_path: Path) -> bool:
    if not _kasa_needs_sync(state, cache_path):
        return False
    mgr = state.kasa_mgr
    cached = _cached_kasa_macs(cache_path)
    _LOGGER.info(
        "Kasa discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_kasa_macs(mgr, cache_path)),
        sorted(cached),
    )
    ok = await mgr.reload_from_cache()
    if not ok:
        _mark_failed(DeviceFamilyId.KASA, cached)
        return False
    _clear_failed(DeviceFamilyId.KASA)
    return True


async def _sync_sonos(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.sonos_mgr
    if mgr is None or not _sonos_needs_sync(state, cache_path):
        return False
    cached = _cached_sonos_uids(cache_path)
    _LOGGER.info(
        "Sonos discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_sonos_uids(mgr)),
        sorted(cached),
    )
    ok = await mgr.reload_from_cache()
    if not ok:
        _mark_failed(DeviceFamilyId.SONOS, cached)
        return False
    _clear_failed(DeviceFamilyId.SONOS)
    return True


async def _sync_tailwind(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.tailwind_mgr
    if mgr is None or not _tailwind_needs_sync(state, cache_path):
        return False
    cached = _cached_tailwind_hub_macs(cache_path)
    _LOGGER.info(
        "Tailwind discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_tailwind_hub_macs(mgr)),
        sorted(cached),
    )
    ok = await mgr.reload_from_cache(cache_path=cache_path)
    if not ok:
        _mark_failed(DeviceFamilyId.TAILWIND, cached)
        return False
    _clear_failed(DeviceFamilyId.TAILWIND)
    return True


async def _sync_vizio(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.vizio_mgr
    if mgr is None or not _vizio_needs_sync(state, cache_path):
        return False
    cached = _cached_vizio_ids(cache_path, mgr)
    _LOGGER.info(
        "Vizio discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_vizio_ids(mgr)),
        sorted(cached),
    )
    ok = await mgr.reload_from_cache()
    if not ok:
        _mark_failed(DeviceFamilyId.VIZIO, cached)
        return False
    _clear_failed(DeviceFamilyId.VIZIO)
    return True


def _tailwind_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.tailwind_mgr
    if mgr is None:
        return False
    cached = _cached_tailwind_hub_macs(cache_path)
    if not cached:
        return False
    try:
        live = _live_tailwind_hub_macs(mgr)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.TAILWIND)


def _vizio_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.vizio_mgr
    if mgr is None:
        return False
    cached = _cached_vizio_ids(cache_path, mgr)
    if not cached:
        return False
    try:
        live = _live_vizio_ids(mgr)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.VIZIO)
