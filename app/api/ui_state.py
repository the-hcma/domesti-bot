"""Build the ``GET /v1/ui/state`` payload from live device managers + SQLite.

This module is the single place that knows how to map a
:class:`app.device_manager_cli.DeviceManagersState` (live, in-memory) plus
the persisted ``ui_preferences`` SQLite rows into the ``UIStateOut`` shape
returned by the HTTP API.

The mapping is intentionally read-only:

* It never touches the network — every value comes from cached state set by
  the manager's previous ``fetch()``. Callers that need a fresh reading
  must invoke ``fetch()`` separately before calling :func:`build_ui_state`.
* It never mutates the SQLite store — the preferences read here is
  ``load_ui_preferences``; the writer paths (toggle endpoints) land in
  later PRs (PR4 for kasa, PR5 for tailwind).

Family colors and labels are owned by this module so the same palette
renders identically across the web UI, future native clients, and any
future embed surface. Each entry in :data:`_FAMILIES` is one row of tiles.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from app import kasa_discovery_store
from app.api.schemas import UIDeviceOut, UIFamilyOut, UIStateOut
from app.device_manager_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_engine import DoorPosition, SwitchPowerState

# Server-owned UI metadata per family. Order in this list is the rendering
# order on the page (top → bottom rows of tiles).
_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("kasa", "Lights & plugs", "#3B82F6"),
    ("tailwind", "Garage doors", "#10B981"),
)


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
