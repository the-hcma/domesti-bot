"""Sync in-memory device managers from the shared discovery SQLite cache.

The CLI ``refresh-discovery`` path writes ``device_discovery.sqlite`` while the
HTTP process keeps live ``*DeviceManager`` maps. Callers
(``GET /v1/ui/state``) invoke :func:`maybe_sync_discovery_cache` so the UI
roster tracks cache drift without LAN rediscovery (UDP / mDNS / SSDP).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app import device_discovery_store
from app.device_enums import DeviceFamilyId
from app.device_manager import NotInitializedError
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_credentials import resolve_vizio_auth_token, vizio_device_id_from_parts
from app.vizio_device_manager import VizioDeviceManager

_LOGGER = logging.getLogger(__name__)

_FP_SEP = "\x1f"


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
    if not known or not all(uid for _h, _p, _fn, uid, _m in known):
        return frozenset()
    return frozenset(uid for _h, _p, _fn, uid, _m in known if uid)


def _cached_kasa_hosts(cache_path: Path) -> frozenset[str]:
    return frozenset(
        host
        for host, _alias, _cfg, _requires_klap in device_discovery_store.load_cached_configs(
            cache_path
        )
    )


def _cached_sonos_keys(cache_path: Path) -> frozenset[str]:
    return frozenset(
        _fp(uid, host)
        for uid, host, _name in device_discovery_store.load_sonos_zones(cache_path)
        if uid.strip() and host.strip()
    )


def _cached_tailwind_hosts(cache_path: Path) -> frozenset[str]:
    host = (device_discovery_store.load_tailwind_host(cache_path) or "").strip()
    return frozenset({host} if host else ())


def _cached_vizio_ids(cache_path: Path, mgr: VizioDeviceManager) -> frozenset[str]:
    """IDs that ``reload_from_cache`` would attempt (token-backed endpoints only)."""

    ids: set[str] = set()
    for host, port, _display, _model, mac, _diid in device_discovery_store.load_vizio_tvs(
        cache_path
    ):
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


def _fp(*parts: str) -> str:
    return _FP_SEP.join(parts)


def _kasa_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    cached = _cached_kasa_hosts(cache_path)
    if not cached:
        return False
    try:
        live = _live_kasa_accounted_hosts(state.kasa_mgr)
    except NotInitializedError:
        return False
    if cached == live:
        return False
    return cached != _failed_fp(DeviceFamilyId.KASA)


def _live_androidtv_uuids(mgr: object) -> frozenset[str]:
    switches = getattr(mgr, "switches")
    return frozenset(str(dev.identifier).strip() for dev in switches if str(dev.identifier).strip())


def _live_kasa_accounted_hosts(mgr: KasaDeviceManager) -> frozenset[str]:
    """Hosts the manager already knows about (connected or KLAP-skipped)."""

    hosts = {(kd._kDevice.host or "").strip() for kd in mgr.switches}
    hosts.discard("")
    hosts.update(mgr.skipped_auth_hosts)
    return frozenset(hosts)


def _live_sonos_keys(mgr: SonosDeviceManager) -> frozenset[str]:
    keys: set[str] = set()
    for sd in mgr.players:
        uid = (sd.identifier or "").strip()
        host = (getattr(sd._soco, "ip_address", None) or "").strip()
        if uid and host:
            keys.add(_fp(uid, host))
    return frozenset(keys)


def _live_tailwind_hosts(mgr: GotailwindDeviceManager) -> frozenset[str]:
    host = (mgr.host or "").strip()
    return frozenset({host} if host else ())


def _live_vizio_ids(mgr: VizioDeviceManager) -> frozenset[str]:
    return frozenset(tv.identifier for tv in mgr.tvs)


def _mark_failed(family: DeviceFamilyId, fingerprint: frozenset[str]) -> None:
    runtime.discovery_cache_sync_failed[family.value] = fingerprint


def _sonos_needs_sync(state: DeviceManagersState, cache_path: Path) -> bool:
    mgr = state.sonos_mgr
    if mgr is None:
        return False
    cached = _cached_sonos_keys(cache_path)
    if not cached:
        return False
    try:
        live = _live_sonos_keys(mgr)
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
    cached = _cached_kasa_hosts(cache_path)
    _LOGGER.info(
        "Kasa discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_kasa_accounted_hosts(mgr)),
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
    cached = _cached_sonos_keys(cache_path)
    _LOGGER.info(
        "Sonos discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_sonos_keys(mgr)),
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
    cached = _cached_tailwind_hosts(cache_path)
    _LOGGER.info(
        "Tailwind discovery cache drift: live=%s cache=%s; reloading from SQLite",
        sorted(_live_tailwind_hosts(mgr)),
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
    cached = _cached_tailwind_hosts(cache_path)
    if not cached:
        return False
    try:
        live = _live_tailwind_hosts(mgr)
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
