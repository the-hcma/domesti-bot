"""Build the ``GET /v1/ui/state`` payload + the per-device / bulk action helpers.

This module is the single place that knows how to map a
:class:`app.device_manager_cli.DeviceManagersState` (live, in-memory) plus
the persisted ``ui_preferences`` SQLite rows into the ``UIStateOut`` shape
returned by the HTTP API, *and* the helpers that mutate device state via
the kasa stack.

The read path (:func:`build_ui_state`, :func:`build_kasa_device_view`) is
intentionally network-free: every value comes from cached state set by the
manager's previous ``fetch()``. Callers that need a fresh reading must
invoke ``fetch()`` separately.

The write path (:func:`bulk_off_global_apply`, :func:`bulk_off_kasa_apply`)
*does* fire ``await kd.turn_off()`` for every targeted device. It does
**not** mutate ``ui_preferences`` — those are written through
:mod:`kasa_discovery_store.upsert_ui_preference` from the route handlers.

Family colors and labels are owned by this module so the same palette
renders identically across the web UI, future native clients, and any
future embed surface.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app import kasa_discovery_store
from app.api.schemas import UIDeviceOut, UIFamilyOut, UIStateOut
from app.device_manager_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDevice, GotailwindDeviceManager
from app.kasa_device_manager import KasaDevice, KasaDeviceManager
from app.rule_engine import DoorPosition, SwitchPowerState

# Server-owned UI metadata per family. Order in this list is the rendering
# order on the page (top → bottom rows of tiles).
_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("kasa", "Lights & plugs", "#3B82F6"),
    ("tailwind", "Garage doors", "#10B981"),
)


async def _bulk_close_tailwind_apply_impl(
    mgr: GotailwindDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Iterate tailwind doors, close non-excluded ones, return ``(affected, skipped)``.

    Doors that are *already* closed (``is_closed=True``) are still passed
    to ``close()`` — the underlying tailwind controller treats it as a
    no-op and the explicit call keeps the success/failure path uniform
    for the UI. Doors in a transient state (``OPENING`` / ``CLOSING``)
    are also closed; the controller will queue the new command.
    """

    affected: list[str] = []
    skipped: list[str] = []
    for gd in mgr.doors:
        key = gd.identifier
        if key in excluded:
            skipped.append(key)
            continue
        await gd.close()
        affected.append(key)
    affected.sort()
    skipped.sort()
    return affected, skipped


async def _bulk_off_kasa_apply_impl(
    mgr: KasaDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Iterate kasa switches, turn off non-excluded ones, return ``(affected, skipped)``.

    ``affected`` is the host list the helper called ``turn_off`` on (in
    sorted order); ``skipped`` is the excluded subset (also sorted). Hosts
    that are blank/whitespace are dropped silently — they can't be
    addressed and were already filtered out of :func:`build_ui_state`.
    """

    affected: list[str] = []
    skipped: list[str] = []
    for kd in mgr.switches:
        host = (kd._kDevice.host or "").strip()
        if not host:
            continue
        if host in excluded:
            skipped.append(host)
            continue
        await kd.turn_off()
        affected.append(host)
    affected.sort()
    skipped.sort()
    return affected, skipped


def _door_state(is_open: bool, is_closed: bool) -> str:
    if is_open:
        return DoorPosition.OPEN.value
    if is_closed:
        return DoorPosition.CLOSED.value
    return "unknown"


def _excluded_keys(
    rows: Iterable[tuple[str, str, bool]], backend: str
) -> set[str]:
    return {key for be, key, exclude in rows if be == backend and exclude}


def _kasa_devices(
    mgr: KasaDeviceManager,
    excluded: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per *unique* kasa device (host-deduped).

    ``mgr.switches`` already de-duplicates by ``id()``; the ``host`` is the
    canonical key for both ``ui_preferences`` and the API payload.
    """

    out: list[UIDeviceOut] = []
    for kd in mgr.switches:
        host = (kd._kDevice.host or "").strip()
        if not host:
            continue
        out.append(
            UIDeviceOut(
                id=host,
                family_id="kasa",
                label=kd.preferred_label,
                kind="switch",
                state=_switch_state(kd.is_on),
                exclude_from_global=host in excluded,
            )
        )
    out.sort(key=lambda d: (d.label.lower(), d.id))
    return out


def _switch_state(is_on: bool) -> str:
    return SwitchPowerState.ON.value if is_on else SwitchPowerState.OFF.value


def _tailwind_devices(
    mgr: GotailwindDeviceManager,
    excluded: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per Tailwind door.

    Canonical key is the door's ``identifier`` (matches
    :func:`app.device_manager_cli._sqlite_canonical_key` for the
    ``tailwind`` backend). A door reporting neither fully open nor fully
    closed (e.g. ``OPENING`` / ``CLOSING``) becomes ``state="unknown"`` so
    the UI never has to guess.
    """

    out: list[UIDeviceOut] = []
    for gd in mgr.doors:
        key = gd.identifier
        out.append(
            UIDeviceOut(
                id=key,
                family_id="tailwind",
                label=gd.preferred_label,
                kind="door",
                state=_door_state(gd.is_open, gd.is_closed),
                exclude_from_global=key in excluded,
            )
        )
    out.sort(key=lambda d: (d.label.lower(), d.id))
    return out


def build_kasa_device_view(
    mgr: KasaDeviceManager,
    *,
    host: str,
    cache_path: Path | None,
) -> UIDeviceOut:
    """Build a fresh :class:`UIDeviceOut` for one kasa device after an action.

    Re-reads the ``ui_preferences`` row each call so a toggle endpoint
    can return the exclusion flag without the caller hand-passing it.
    Raises :class:`KeyError` when the host doesn't match a known device
    (the route handler maps that to a 404).
    """

    kd = find_kasa_by_host(mgr, host)
    if kd is None:
        raise KeyError(host)
    excluded = (
        _excluded_keys(
            kasa_discovery_store.load_ui_preferences(cache_path), "kasa"
        )
        if cache_path is not None
        else set()
    )
    return UIDeviceOut(
        id=host,
        family_id="kasa",
        label=kd.preferred_label,
        kind="switch",
        state=_switch_state(kd.is_on),
        exclude_from_global=host in excluded,
    )


def build_tailwind_device_view(
    mgr: GotailwindDeviceManager,
    *,
    device_id: str,
    cache_path: Path | None,
) -> UIDeviceOut:
    """Build a fresh :class:`UIDeviceOut` for one tailwind door after an action.

    Symmetric to :func:`build_kasa_device_view`. Raises :class:`KeyError`
    when ``device_id`` doesn't match a known door (the route handler maps
    that to a 404).
    """

    gd = find_tailwind_by_identifier(mgr, device_id)
    if gd is None:
        raise KeyError(device_id)
    excluded = (
        _excluded_keys(
            kasa_discovery_store.load_ui_preferences(cache_path), "tailwind"
        )
        if cache_path is not None
        else set()
    )
    return UIDeviceOut(
        id=device_id,
        family_id="tailwind",
        label=gd.preferred_label,
        kind="door",
        state=_door_state(gd.is_open, gd.is_closed),
        exclude_from_global=device_id in excluded,
    )


def build_ui_state(
    state: DeviceManagersState,
    *,
    cache_path: Path | None,
) -> UIStateOut:
    """Assemble the ``UIStateOut`` for the live :class:`DeviceManagersState`.

    ``cache_path`` is the SQLite discovery cache path (same value already
    threaded through the CLI as ``--discovery-cache`` and surfaced on
    ``DeviceManagersState.cache_path``). When ``None`` (e.g.
    ``--no-discovery-cache``), preferences load returns an empty list and
    every device defaults to ``exclude_from_global=False``.

    Empty families (e.g. user passed ``--no-tailwind`` so
    ``state.tailwind_mgr is None``, or the kasa sweep found nothing) are
    omitted from the payload.
    """

    pref_rows = (
        kasa_discovery_store.load_ui_preferences(cache_path)
        if cache_path is not None
        else []
    )
    families: list[UIFamilyOut] = []
    for family_id, label, color in _FAMILIES:
        excluded = _excluded_keys(pref_rows, family_id)
        if family_id == "kasa":
            devices = _kasa_devices(state.kasa_mgr, excluded)
        elif family_id == "tailwind" and state.tailwind_mgr is not None:
            devices = _tailwind_devices(state.tailwind_mgr, excluded)
        else:
            devices = []
        if not devices:
            continue
        families.append(
            UIFamilyOut(id=family_id, label=label, color=color, devices=devices)
        )
    return UIStateOut(families=families)


async def bulk_close_tailwind_apply(
    state: DeviceManagersState,
) -> tuple[list[str], list[str]]:
    """Family-level "close all tailwind doors" — ignores per-device exclusions.

    Returns ``(affected, skipped)`` where ``affected`` is the door
    identifiers we called ``close()`` on and ``skipped`` is empty (the
    family bulk ignores ``exclude_from_global``). When the manager isn't
    configured (``state.tailwind_mgr is None``), both lists are empty.
    """

    if state.tailwind_mgr is None:
        return [], []
    return await _bulk_close_tailwind_apply_impl(
        state.tailwind_mgr, excluded=set()
    )


async def bulk_off_global_apply(
    state: DeviceManagersState,
    *,
    cache_path: Path | None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Global "turn off / close all": kasa **and** tailwind, honoring exclusions.

    Returns ``(affected, skipped)`` where each entry is a
    ``(family_id, device_id)`` tuple — ``family_id`` is needed because
    the global action spans families. When ``cache_path`` is ``None``
    (``--no-discovery-cache``) we treat every device as **not** excluded,
    which matches the read-side behavior of :func:`build_ui_state`.
    """

    rows = (
        kasa_discovery_store.load_ui_preferences(cache_path)
        if cache_path is not None
        else []
    )
    kasa_excluded = _excluded_keys(rows, "kasa")
    tailwind_excluded = _excluded_keys(rows, "tailwind")
    affected: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    kasa_aff, kasa_skip = await _bulk_off_kasa_apply_impl(
        state.kasa_mgr, excluded=kasa_excluded
    )
    affected.extend(("kasa", k) for k in kasa_aff)
    skipped.extend(("kasa", k) for k in kasa_skip)
    if state.tailwind_mgr is not None:
        tw_aff, tw_skip = await _bulk_close_tailwind_apply_impl(
            state.tailwind_mgr, excluded=tailwind_excluded
        )
        affected.extend(("tailwind", k) for k in tw_aff)
        skipped.extend(("tailwind", k) for k in tw_skip)
    affected.sort()
    skipped.sort()
    return affected, skipped


async def bulk_off_kasa_apply(
    state: DeviceManagersState,
) -> tuple[list[str], list[str]]:
    """Family-level "all kasa off" — ignores per-device exclusions.

    The user clicked an in-family bulk button, so per-device
    ``exclude_from_global`` is intentionally ignored. ``skipped`` is
    therefore always empty in practice (kept in the signature so callers
    don't have to special-case the return shape).
    """

    return await _bulk_off_kasa_apply_impl(state.kasa_mgr, excluded=set())


def find_kasa_by_host(mgr: KasaDeviceManager, host: str) -> KasaDevice | None:
    """Look up a kasa device by its **host** (the canonical key).

    ``KasaDeviceManager.get_device_by_alias`` indexes by
    :attr:`KasaDevice.identifier` (the kasa-reported alias when present,
    otherwise the host) — not the host directly. The UI layer only ever
    receives the host (as ``UIDeviceOut.id``), so it needs this dedicated
    lookup.
    """

    needle = host.strip()
    if not needle:
        return None
    for kd in mgr.switches:
        if (kd._kDevice.host or "").strip() == needle:
            return kd
    return None


def find_tailwind_by_identifier(
    mgr: GotailwindDeviceManager, device_id: str
) -> GotailwindDevice | None:
    """Look up a Tailwind door by its ``identifier`` (the canonical key).

    Mirrors :func:`find_kasa_by_host`. ``mgr.get_device_by_alias`` accepts
    both the identifier and the display label; we restrict to the
    identifier here so the UI never accidentally addresses two doors
    that share a label.
    """

    needle = device_id.strip()
    if not needle:
        return None
    for gd in mgr.doors:
        if gd.identifier == needle:
            return gd
    return None
