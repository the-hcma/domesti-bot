"""Build the ``GET /v1/ui/state`` payload + the per-device / bulk action helpers.

This module is the single place that knows how to map a
:class:`app.domesti_bot_cli.DeviceManagersState` (live, in-memory) plus
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
:func:`device_discovery_store.upsert_ui_preference` from the route handlers.

Family colors and labels are owned by this module so the same palette
renders identically across the web UI, future native clients, and any
future embed surface.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

from app import device_discovery_store
from app.api.schemas import UIDeviceOut, UIFamilyOut, UIOperatorAlertOut, UISonosStreamFavoriteOut, UIStateOut
from app.device_enums import DeviceConditionState, DeviceFamilyId
from app.device_manager import NotInitializedError
from app.domesti_bot_cli import DeviceManagersState
from app.expected_device_change import mark_expected_device_change
from app.gotailwind_device_manager import GotailwindDevice, GotailwindDeviceManager
from app.kasa_device_manager import KasaDevice, KasaDeviceManager
from app.operator_alerts import operator_alert_store
from app.sonos_device_manager import (
    SonosDeviceManager,
    SonosSpeakerDevice,
    SonosTransitionUnavailableError,
)
from app.ui_compact_icon import resolve_compact_icon
from app.vizio_device_manager import VizioDeviceManager, VizioTvDevice

_LOGGER = logging.getLogger(__name__)

# Server-owned UI metadata per family. Order in this list is the rendering
# order on the page (top → bottom rows of tiles); documented on ``UIStateOut``.
# TODO(ep1/#521): add ("ep1", "Everything Presence One", …) when Ep1DeviceManager
# ships — without it, EP1 devices would be discovered but omitted from /v1/ui/state.
_FAMILIES: tuple[tuple[str, str, str], ...] = (
    ("kasa", "Lights & plugs", "#3B82F6"),
    ("sonos", "Sonos zones", "#8B5CF6"),
    ("vizio", "Vizio TVs", "#F97316"),
    ("tailwind", "Garage doors", "#10B981"),
)


async def _bulk_close_tailwind_apply_impl(
    mgr: GotailwindDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Iterate tailwind doors, close non-excluded ones, return ``(affected, skipped)``.

    Doors that are already closed are skipped — nothing to do. Doors in a
    transient state (``OPENING`` / ``CLOSING``) or open are passed to
    ``close()``; :meth:`GotailwindDevice.close` swallows
    ``TailwindDoorAlreadyInStateError`` when the controller reports the
    door is already shut.
    """

    affected: list[str] = []
    skipped: list[str] = []
    for gd in mgr.doors:
        key = gd.identifier
        if key in excluded:
            skipped.append(key)
            continue
        if gd.is_closed:
            continue
        mark_expected_device_change(DeviceFamilyId.TAILWIND, key)
        await gd.close()
        affected.append(key)
    affected.sort()
    skipped.sort()
    return affected, skipped


async def _bulk_pause_sonos_apply_impl(
    mgr: SonosDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Iterate Sonos zones, pause non-excluded *playing* ones, return ``(affected, skipped)``.

    Zones with ``is_playing is False`` (already paused) are left alone.
    Zones with ``is_playing is None`` (no poll yet) still get a
    ``pause`` attempt so global all-off can catch a zone that is
    playing before the first watcher tick. Excluded zones are reported
    in ``skipped`` even when they are already paused, matching the
    kasa helper's convention so the UI can honestly say "X devices
    weren't touched because you excluded them".

    A zone that raises :class:`SonosTransitionUnavailableError` (UPnP
    701 — the zone drifted out of ``PLAYING`` between our last poll
    and this call) is logged at warning and dropped from both lists:
    the zone isn't truly excluded (the user didn't ask for it to be),
    and we didn't actually pause it either. One stuck zone must not
    take down a global "Turn off / pause / close everything" action.
    """

    affected: list[str] = []
    skipped: list[str] = []
    for sp in mgr.players:
        key = sp.identifier
        if key in excluded:
            skipped.append(key)
            continue
        if sp.is_playing is False:
            continue
        mark_expected_device_change(DeviceFamilyId.SONOS, key)
        try:
            await sp.pause()
        except SonosTransitionUnavailableError as exc:
            _LOGGER.warning(
                "[ui bulk-pause] %s: skipping zone, Sonos refused pause (%s)",
                key,
                exc,
            )
            continue
        affected.append(key)
    affected.sort()
    skipped.sort()
    return affected, skipped


async def _bulk_off_vizio_apply_impl(
    mgr: VizioDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    affected: list[str] = []
    skipped: list[str] = []
    for tv in mgr.tvs:
        device_id = tv.identifier
        if device_id in excluded:
            skipped.append(device_id)
            continue
        if not tv.is_on:
            continue
        mark_expected_device_change(DeviceFamilyId.VIZIO, device_id)
        await tv.turn_off()
        affected.append(device_id)
    affected.sort()
    skipped.sort()
    return affected, skipped


async def _bulk_off_kasa_apply_impl(
    mgr: KasaDeviceManager,
    *,
    excluded: set[str],
) -> tuple[list[str], list[str]]:
    """Iterate kasa switches, turn off non-excluded ones, return ``(affected, skipped)``.

    ``affected`` is the canonical id list the helper called ``turn_off`` on because
    ``is_on`` was true (already-off switches are omitted). ``skipped`` is
    the excluded subset (also sorted). Blank identifiers are dropped silently
    (identifiers are MACs when known, else hosts).
    When Kasa discovery failed at boot (``NotInitializedError``), both
    lists are empty — same as an empty switch set.
    """

    affected: list[str] = []
    skipped: list[str] = []
    try:
        switches = mgr.switches
    except NotInitializedError:
        return [], []
    for kd in switches:
        key = (kd.identifier or "").strip()
        if not key:
            continue
        if key in excluded:
            skipped.append(key)
            continue
        if not kd.is_on:
            continue
        mark_expected_device_change(DeviceFamilyId.KASA, key)
        await kd.turn_off()
        affected.append(key)
    affected.sort()
    skipped.sort()
    return affected, skipped


def _compact_icon_for_device(
    *,
    family_id: str,
    label: str,
    kind: str,
    kasa_model: str | None = None,
) -> str:
    return resolve_compact_icon(
        family_id=family_id,
        label=label,
        kind=kind,
        kasa_model=kasa_model,
    )


def _door_state(is_open: bool, is_closed: bool) -> str:
    if is_open:
        return DeviceConditionState.OPEN.value
    if is_closed:
        return DeviceConditionState.CLOSED.value
    return "unknown"


def _excluded_keys(rows: Iterable[tuple[str, str, bool, bool]], backend: str) -> set[str]:
    return {key for be, key, exclude, _hide in rows if be == backend and exclude}


def _hidden_on_mobile_keys(rows: Iterable[tuple[str, str, bool, bool]], backend: str) -> set[str]:
    return {key for be, key, _exclude, hide in rows if be == backend and hide}


def _identity_details_kasa(kd: KasaDevice) -> list[str]:
    details: list[str] = []
    model = _kasa_hardware_model(kd)
    if model:
        details.append(f"model: {model}")
    preferred = getattr(kd, "preferred_label", None) or ""
    identifier = getattr(kd, "identifier", None) or ""
    if preferred and preferred != identifier:
        details.append(f"alias: {preferred}")
    return details


def _identity_details_sonos(sp: SonosSpeakerDevice) -> list[str]:
    rincon = (getattr(sp, "rincon_uid", None) or "").strip()
    if not rincon:
        return []
    return [f"RINCON: {rincon}"]


def _identity_details_tailwind(gd: GotailwindDevice) -> list[str]:
    details: list[str] = []
    door_index = getattr(gd, "door_index", None)
    if door_index is not None:
        details.append(f"door index: {door_index}")
    door_key = (getattr(gd, "door_key", None) or "").strip()
    identifier = (getattr(gd, "identifier", None) or "").strip()
    if door_key and door_key != identifier:
        details.append(f"door id: {door_key}")
    return details


def _identity_details_vizio(tv: VizioTvDevice) -> list[str]:
    details: list[str] = []
    endpoint = getattr(tv, "endpoint", None)
    if endpoint is None:
        return details
    model = (getattr(endpoint, "model", None) or "").strip()
    if model:
        details.append(f"model: {model}")
    diid = (getattr(endpoint, "diid", None) or "").strip()
    if diid:
        details.append(f"diid: {diid}")
    return details


def _optional_host(value: object | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _kasa_hardware_model(kd: KasaDevice) -> str | None:
    raw = getattr(getattr(kd, "_kDevice", None), "model", None)
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _kasa_devices(
    mgr: KasaDeviceManager,
    excluded: set[str],
    hidden_on_mobile: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per *unique* kasa device.

    Canonical key is the MAC address — matching ``ui_preferences.canonical_key``.
    Devices without a learned MAC address are omitted.

    When bootstrap left the manager unfetched (Kasa family failed while
    other backends succeeded), treat the family as empty so
    ``GET /v1/ui/state`` stays usable instead of raising ``500``.
    """

    out: list[UIDeviceOut] = []
    try:
        switches = mgr.switches
    except NotInitializedError:
        return []
    for kd in switches:
        key = (kd.identifier or "").strip()
        if not key:
            continue
        out.append(
            UIDeviceOut(
                id=key,
                family_id="kasa",
                label=kd.preferred_label,
                kind="switch",
                state=_switch_state(kd.is_on),
                compact_icon=_compact_icon_for_device(
                    family_id="kasa",
                    label=kd.preferred_label,
                    kind="switch",
                    kasa_model=_kasa_hardware_model(kd),
                ),
                mac_address=kd.mac_address,
                host=_optional_host(getattr(kd, "host", None)),
                identity_details=_identity_details_kasa(kd),
                exclude_from_global=key in excluded,
                hide_on_mobile=key in hidden_on_mobile,
            )
        )
    out.sort(key=lambda d: (d.label.lower(), d.id))
    return out


def _sonos_devices(
    mgr: SonosDeviceManager,
    excluded: set[str],
    hidden_on_mobile: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per Sonos zone.

    Canonical key is the zone's MAC address (from ``RINCON_…`` or ARP). ``state`` is
    derived from the cached :attr:`SonosSpeakerDevice.is_playing` flag —
    ``None`` (no poll yet) becomes ``"unknown"`` so the UI never blocks
    on a live UPnP call just to render a tile.
    """

    out: list[UIDeviceOut] = []
    for sp in mgr.players:
        key = sp.identifier
        out.append(
            UIDeviceOut(
                id=key,
                family_id="sonos",
                label=sp.preferred_label,
                kind="speaker",
                state=_sonos_state(sp.is_playing),
                compact_icon=_compact_icon_for_device(
                    family_id="sonos",
                    label=sp.preferred_label,
                    kind="speaker",
                ),
                mac_address=sp.mac_address,
                host=_optional_host(getattr(sp, "host", None)),
                identity_details=_identity_details_sonos(sp),
                exclude_from_global=key in excluded,
                hide_on_mobile=key in hidden_on_mobile,
                stream_favorites=_sonos_stream_favorites_out(sp),
            )
        )
    out.sort(key=lambda d: (d.label.lower(), d.id))
    return out


def _sonos_stream_favorites_out(
    sp: SonosSpeakerDevice,
) -> list[UISonosStreamFavoriteOut]:
    favorites = getattr(sp, "stream_favorites", ())
    return [UISonosStreamFavoriteOut(name=favorite.name, uri=favorite.uri) for favorite in favorites]


def _sonos_state(is_playing: bool | None) -> str:
    if is_playing is True:
        return DeviceConditionState.PLAYING
    if is_playing is False:
        return DeviceConditionState.PAUSED
    return "unknown"


def _switch_state(is_on: bool) -> str:
    return DeviceConditionState.ON.value if is_on else DeviceConditionState.OFF.value


def _vizio_switch_state(tv: VizioTvDevice) -> str:
    return tv.ui_power_state()


def _tailwind_devices(
    mgr: GotailwindDeviceManager,
    excluded: set[str],
    hidden_on_mobile: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per Tailwind door.

    Canonical key is the door's ``identifier`` (matches
    :func:`app.domesti_bot_cli._sqlite_canonical_key` for the
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
                compact_icon=_compact_icon_for_device(
                    family_id="tailwind",
                    label=gd.preferred_label,
                    kind="door",
                ),
                mac_address=gd.mac_address,
                host=_optional_host(getattr(mgr, "host", None)),
                identity_details=_identity_details_tailwind(gd),
                exclude_from_global=key in excluded,
                hide_on_mobile=key in hidden_on_mobile,
            )
        )
    out.sort(key=lambda d: (d.label.lower(), d.id))
    return out


def _vizio_devices(
    mgr: VizioDeviceManager,
    excluded: set[str],
    hidden_on_mobile: set[str],
) -> list[UIDeviceOut]:
    """One :class:`UIDeviceOut` per Vizio TV."""

    out: list[UIDeviceOut] = []
    for tv in mgr.tvs:
        key = tv.identifier
        out.append(
            UIDeviceOut(
                id=key,
                family_id="vizio",
                label=tv.preferred_label,
                kind="switch",
                state=_vizio_switch_state(tv),
                compact_icon=_compact_icon_for_device(
                    family_id="vizio",
                    label=tv.preferred_label,
                    kind="switch",
                ),
                mac_address=tv.mac_address,
                host=_optional_host(getattr(getattr(tv, "endpoint", None), "host", None)),
                identity_details=_identity_details_vizio(tv),
                exclude_from_global=key in excluded,
                hide_on_mobile=key in hidden_on_mobile,
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
    Raises :class:`KeyError` when the host/MAC doesn't match a known device
    (the route handler maps that to a 404).
    """

    kd = find_kasa_by_id(mgr, host)
    if kd is None:
        raise KeyError(host)
    pref_rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    excluded = _excluded_keys(pref_rows, "kasa")
    hidden = _hidden_on_mobile_keys(pref_rows, "kasa")
    key = kd.identifier
    return UIDeviceOut(
        id=key,
        family_id="kasa",
        label=kd.preferred_label,
        kind="switch",
        state=_switch_state(kd.is_on),
        compact_icon=_compact_icon_for_device(
            family_id="kasa",
            label=kd.preferred_label,
            kind="switch",
            kasa_model=_kasa_hardware_model(kd),
        ),
        mac_address=kd.mac_address,
        host=_optional_host(getattr(kd, "host", None)),
        identity_details=_identity_details_kasa(kd),
        exclude_from_global=key in excluded,
        hide_on_mobile=key in hidden,
    )


def build_sonos_device_view(
    mgr: SonosDeviceManager,
    *,
    device_id: str,
    cache_path: Path | None,
) -> UIDeviceOut:
    """Build a fresh :class:`UIDeviceOut` for one Sonos zone after an action.

    Symmetric to :func:`build_kasa_device_view`. Raises :class:`KeyError`
    when ``device_id`` doesn't match a known zone (the route handler
    maps that to a 404). Reads the cached
    :attr:`SonosSpeakerDevice.is_playing` rather than triggering a live
    UPnP call — the action handler updates the cache before this
    builds, so the returned state already reflects the new playback.
    """

    sp = find_sonos_by_identifier(mgr, device_id)
    if sp is None:
        raise KeyError(device_id)
    pref_rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    excluded = _excluded_keys(pref_rows, "sonos")
    hidden = _hidden_on_mobile_keys(pref_rows, "sonos")
    return UIDeviceOut(
        id=device_id,
        family_id="sonos",
        label=sp.preferred_label,
        kind="speaker",
        state=_sonos_state(sp.is_playing),
        compact_icon=_compact_icon_for_device(
            family_id="sonos",
            label=sp.preferred_label,
            kind="speaker",
        ),
        mac_address=sp.mac_address,
        host=_optional_host(getattr(sp, "host", None)),
        identity_details=_identity_details_sonos(sp),
        exclude_from_global=device_id in excluded,
        hide_on_mobile=device_id in hidden,
        stream_favorites=_sonos_stream_favorites_out(sp),
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
    pref_rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    excluded = _excluded_keys(pref_rows, "tailwind")
    hidden = _hidden_on_mobile_keys(pref_rows, "tailwind")
    return UIDeviceOut(
        id=device_id,
        family_id="tailwind",
        label=gd.preferred_label,
        kind="door",
        state=_door_state(gd.is_open, gd.is_closed),
        compact_icon=_compact_icon_for_device(
            family_id="tailwind",
            label=gd.preferred_label,
            kind="door",
        ),
        mac_address=gd.mac_address,
        host=_optional_host(getattr(mgr, "host", None)),
        identity_details=_identity_details_tailwind(gd),
        exclude_from_global=device_id in excluded,
        hide_on_mobile=device_id in hidden,
    )


def build_vizio_device_view(
    mgr: VizioDeviceManager,
    *,
    device_id: str,
    cache_path: Path | None,
) -> UIDeviceOut:
    """Build a fresh :class:`UIDeviceOut` for one Vizio TV after an action."""

    tv = find_vizio_by_id(mgr, device_id)
    if tv is None:
        raise KeyError(device_id)
    pref_rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    excluded = _excluded_keys(pref_rows, "vizio")
    hidden = _hidden_on_mobile_keys(pref_rows, "vizio")
    return UIDeviceOut(
        id=device_id,
        family_id="vizio",
        label=tv.preferred_label,
        kind="switch",
        state=_vizio_switch_state(tv),
        compact_icon=_compact_icon_for_device(
            family_id="vizio",
            label=tv.preferred_label,
            kind="switch",
        ),
        mac_address=tv.mac_address,
        host=_optional_host(getattr(getattr(tv, "endpoint", None), "host", None)),
        identity_details=_identity_details_vizio(tv),
        exclude_from_global=device_id in excluded,
        hide_on_mobile=device_id in hidden,
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
    every device defaults to ``exclude_from_global=False`` and
    ``hide_on_mobile=False``.

    Empty families (e.g. user passed ``--no-tailwind`` so
    ``state.tailwind_mgr is None``, or the kasa sweep found nothing) are
    omitted from the payload.
    """

    pref_rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    families: list[UIFamilyOut] = []
    for family_id, label, color in _FAMILIES:
        excluded = _excluded_keys(pref_rows, family_id)
        hidden = _hidden_on_mobile_keys(pref_rows, family_id)
        if family_id == "kasa":
            devices = _kasa_devices(state.kasa_mgr, excluded, hidden)
        elif family_id == "sonos" and state.sonos_mgr is not None:
            devices = _sonos_devices(state.sonos_mgr, excluded, hidden)
        elif family_id == "tailwind" and state.tailwind_mgr is not None:
            devices = _tailwind_devices(state.tailwind_mgr, excluded, hidden)
        elif family_id == "vizio" and state.vizio_mgr is not None:
            devices = _vizio_devices(state.vizio_mgr, excluded, hidden)
        else:
            devices = []
        if not devices:
            continue
        families.append(UIFamilyOut(id=family_id, label=label, color=color, devices=devices))
    operator_alert: UIOperatorAlertOut | None = None
    smtp_alert = operator_alert_store.current_smtp_notification_failure()
    if smtp_alert is not None:
        operator_alert = UIOperatorAlertOut(
            message=smtp_alert.message,
            reason_code=smtp_alert.reason_code,
            recorded_at=smtp_alert.recorded_at,
        )
    return UIStateOut(families=families, operator_alert=operator_alert)


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
    return await _bulk_close_tailwind_apply_impl(state.tailwind_mgr, excluded=set())


async def bulk_off_global_apply(
    state: DeviceManagersState,
    *,
    cache_path: Path | None,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Global "turn off / close / pause all": every supported family, honoring exclusions.

    Returns ``(affected, skipped)`` where each entry is a
    ``(family_id, device_id)`` tuple — ``family_id`` is needed because
    the global action spans families. Per-family translation:

    * ``kasa`` → ``turn_off`` for every non-excluded switch.
    * ``sonos`` → ``pause`` for every non-excluded zone that's
      currently playing (paused / unknown zones are left alone).
    * ``tailwind`` → ``close`` for every non-excluded door.
    * ``vizio`` → ``turn_off`` for every non-excluded TV.

    When ``cache_path`` is ``None`` (``--no-discovery-cache``) we treat
    every device as **not** excluded, which matches the read-side
    behavior of :func:`build_ui_state`.
    """

    rows = device_discovery_store.load_ui_preferences(cache_path) if cache_path is not None else []
    kasa_excluded = _excluded_keys(rows, "kasa")
    sonos_excluded = _excluded_keys(rows, "sonos")
    tailwind_excluded = _excluded_keys(rows, "tailwind")
    vizio_excluded = _excluded_keys(rows, "vizio")
    affected: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    kasa_aff, kasa_skip = await _bulk_off_kasa_apply_impl(state.kasa_mgr, excluded=kasa_excluded)
    affected.extend(("kasa", k) for k in kasa_aff)
    skipped.extend(("kasa", k) for k in kasa_skip)
    if state.sonos_mgr is not None:
        son_aff, son_skip = await _bulk_pause_sonos_apply_impl(state.sonos_mgr, excluded=sonos_excluded)
        affected.extend(("sonos", k) for k in son_aff)
        skipped.extend(("sonos", k) for k in son_skip)
    if state.tailwind_mgr is not None:
        tw_aff, tw_skip = await _bulk_close_tailwind_apply_impl(state.tailwind_mgr, excluded=tailwind_excluded)
        affected.extend(("tailwind", k) for k in tw_aff)
        skipped.extend(("tailwind", k) for k in tw_skip)
    if state.vizio_mgr is not None:
        vz_aff, vz_skip = await _bulk_off_vizio_apply_impl(state.vizio_mgr, excluded=vizio_excluded)
        affected.extend(("vizio", k) for k in vz_aff)
        skipped.extend(("vizio", k) for k in vz_skip)
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


async def bulk_off_vizio_apply(
    state: DeviceManagersState,
) -> tuple[list[str], list[str]]:
    """Family-level "turn off all Vizio TVs" — ignores per-device exclusions."""

    if state.vizio_mgr is None:
        return [], []
    return await _bulk_off_vizio_apply_impl(state.vizio_mgr, excluded=set())


async def bulk_pause_sonos_apply(
    state: DeviceManagersState,
) -> tuple[list[str], list[str]]:
    """Family-level "pause all Sonos zones" — ignores per-device exclusions.

    Only currently-playing zones are paused; already-paused or
    unknown-state zones drop out of the iteration without an extra LAN
    round-trip. When the Sonos manager isn't configured
    (``state.sonos_mgr is None``), both lists are empty.
    """

    if state.sonos_mgr is None:
        return [], []
    return await _bulk_pause_sonos_apply_impl(state.sonos_mgr, excluded=set())


def find_kasa_by_host(mgr: KasaDeviceManager, host: str) -> KasaDevice | None:
    """Look up a kasa device by MAC, host, or other lookup key.

    Retained name for call-site compatibility; prefers MAC-primary identity.
    """

    return find_kasa_by_id(mgr, host)


def find_kasa_by_id(mgr: KasaDeviceManager, device_id: str) -> KasaDevice | None:
    """Look up a kasa device by MAC (preferred), host, alias, or label."""

    needle = device_id.strip()
    if not needle:
        return None
    try:
        by_alias = mgr.get_device_by_alias(needle)
    except NotInitializedError:
        return None
    # Require a real string identifier so MagicMock auto-returns from tests
    # (and other non-device objects) do not short-circuit the switches scan.
    # Also confirm ``needle`` is one of this device's identity / label keys so a
    # stale alias map entry cannot return an unrelated device.
    if by_alias is not None and isinstance(getattr(by_alias, "identifier", None), str):
        vendor_alias = (getattr(getattr(by_alias, "_kDevice", None), "alias", None) or "").strip()
        if needle in {
            by_alias.identifier,
            by_alias.host,
            by_alias.mac_address,
            by_alias.preferred_label,
            vendor_alias,
        }:
            return by_alias
    try:
        switches = mgr.switches
    except NotInitializedError:
        return None
    for kd in switches:
        if needle in {kd.identifier, kd.host, kd.mac_address}:
            return kd
    return None


def find_sonos_by_identifier(mgr: SonosDeviceManager, device_id: str) -> SonosSpeakerDevice | None:
    """Look up a Sonos zone by MAC, ``RINCON_…`` UID, or identifier."""

    needle = device_id.strip()
    if not needle:
        return None
    for sp in mgr.players:
        if needle in {sp.identifier, sp.rincon_uid, sp.mac_address}:
            return sp
    return None


def find_vizio_by_id(mgr: VizioDeviceManager, device_id: str) -> VizioTvDevice | None:
    """Look up a Vizio TV by MAC or ``identifier`` (MAC when known)."""

    needle = device_id.strip()
    if not needle:
        return None
    for tv in mgr.tvs:
        if needle in {tv.identifier, tv.mac_address}:
            return tv
    return None


def find_tailwind_by_identifier(mgr: GotailwindDeviceManager, device_id: str) -> GotailwindDevice | None:
    """Look up a Tailwind door by MAC-compound id, door key, or identifier."""

    needle = device_id.strip()
    if not needle:
        return None
    for gd in mgr.doors:
        if needle in {gd.identifier, gd.door_key}:
            return gd
    return None
