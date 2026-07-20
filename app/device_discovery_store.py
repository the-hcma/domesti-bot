"""SQLite persistence for LAN device discovery (all backends share one cache file).

Schema changes are **additive** only: tables and columns are created or synced via
:func:`app.db.schema.bootstrap_schema` from the SQLAlchemy ORM models.
Reads call :func:`ensure_schema_if_exists` so older on-disk files gain new tables and
columns automatically.

The ``androidtv_discovered_hosts`` table name is historical: rows store **Cast** ``(host, port,
friendly_name)`` hints from PyChromecast (typically port **8009**), not ADB.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.db.models import (
    AndroidTvDiscoveredHost,
    DeviceDisplayName,
    Ep1KnownDevice,
    KasaDiscoveredDevice,
    SonosKnownZone,
    TailwindLastHost,
    UiPreference,
    VizioKnownTv,
)
from app.db.schema import bootstrap_schema, ensure_schema_if_exists
from app.db.session import discovery_session, discovery_write
from app.device_mac import try_normalize_mac
from app.vizio_credentials import vizio_device_id_from_parts
from app.vizio_mac import (
    device_id_for_vizio,
    is_vizio_mac_device_id,
)
from app.vizio_smartcast_client import device_id_for, parse_host_spec

_LOGGER = logging.getLogger(__name__)


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the cache database and ensure the schema exists."""
    bootstrap_schema(path)
    resolved = path.expanduser().resolve()
    conn = sqlite3.connect(resolved)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_schema(path: Path) -> None:
    """Apply CREATE TABLE IF NOT EXISTS to an existing file so older DBs gain new tables.

    There is no separate migration runner; additive schema is idempotent via metadata.create_all.
    """
    ensure_schema_if_exists(path)


def load_androidtv_endpoint_rows(path: Path) -> list[tuple[str, int, str | None]]:
    """Return ``(host, port, friendly_name)`` rows ordered by host, port. Missing file → empty."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(AndroidTvDiscoveredHost).order_by(AndroidTvDiscoveredHost.host, AndroidTvDiscoveredHost.port)
        ).all()
        out: list[tuple[str, int, str | None]] = []
        for row in rows:
            label = (row.friendly_name or "").strip() or None
            out.append((row.host, int(row.port), label))
        return out


def load_androidtv_hosts(path: Path) -> list[tuple[str, int]]:
    """Return ``(host, port)`` rows ordered by host. Missing file → empty list."""
    return [(h, p) for h, p, _ in load_androidtv_endpoint_rows(path)]


def load_androidtv_known_devices(
    path: Path,
) -> list[tuple[str, int, str | None, str | None, str | None, str | None]]:
    """Return rows ``(host, port, friendly_name, uuid, model_name, mac)``.

    Rows that pre-date the uuid/model_name migration will have ``uuid IS NULL``
    (and possibly ``model_name IS NULL``). The Cast manager treats a non-empty
    ``uuid`` on **every** row as the trigger for the no-mDNS fast path; a
    single missing/empty UUID falls back to the targeted-mDNS path that
    rewrites the cache. ``mac`` may be ``None`` on legacy rows; managers must
    re-learn a MAC address (ARP) before admitting the device.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(AndroidTvDiscoveredHost).order_by(AndroidTvDiscoveredHost.host, AndroidTvDiscoveredHost.port)
        ).all()
        out: list[tuple[str, int, str | None, str | None, str | None, str | None]] = []
        for row in rows:
            label = (row.friendly_name or "").strip() or None
            uid_s = (row.uuid or "").strip() or None
            model_s = (row.model_name or "").strip() or None
            mac_s = (getattr(row, "mac", None) or "").strip() or None
            out.append((row.host, int(row.port), label, uid_s, model_s, mac_s))
        return out


def load_cached_configs(
    path: Path,
) -> list[tuple[str, str | None, dict[str, Any], bool, str | None]]:
    """Return ``(host, alias, config_dict, requires_klap_auth, mac)`` rows ordered by host.

    Missing file → empty list. Pre-column rows default ``requires_klap_auth`` to
    ``False`` (anonymous LAN) and ``mac`` to ``None`` until a fetch learns them.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(select(KasaDiscoveredDevice).order_by(KasaDiscoveredDevice.host)).all()
        return [
            (
                row.host,
                row.alias,
                json.loads(row.config_json),
                bool(getattr(row, "requires_klap_auth", 0)),
                (getattr(row, "mac", None) or "").strip() or None,
            )
            for row in rows
        ]


def save_androidtv_hosts(
    path: Path,
    rows: Iterable[
        tuple[str, int]
        | tuple[str, int, str | None]
        | tuple[str, int, str | None, str | None]
        | tuple[str, int, str | None, str | None, str | None]
        | tuple[str, int, str | None, str | None, str | None, str | None]
    ],
) -> None:
    """Replace all rows.

    Accepted shapes (additive, backward-compatible):
      * ``(host, port)``
      * ``(host, port, friendly_name)``
      * ``(host, port, friendly_name, uuid)``
      * ``(host, port, friendly_name, uuid, model_name)``
      * ``(host, port, friendly_name, uuid, model_name, mac)``

    Missing trailing values are persisted as ``NULL``. The Cast cache-fast
    path requires every row's ``uuid`` to be non-empty, so callers that want
    the no-mDNS startup must pass the 4-, 5-, or 6-tuple shape.
    """
    now = time.time()
    records: list[tuple[str, int, str | None, str | None, str | None, str | None]] = []
    for r in rows:
        seq: tuple[Any, ...] = tuple(r)
        h = str(seq[0]).strip()
        p = int(seq[1])
        fn = _nonblank_str(seq[2]) if len(seq) > 2 else None
        uid = _nonblank_str(seq[3]) if len(seq) > 3 else None
        model = _nonblank_str(seq[4]) if len(seq) > 4 else None
        mac = try_normalize_mac(str(seq[5])) if len(seq) > 5 and seq[5] else None
        records.append((h, p, fn, uid, model, mac))

    def _write(session: Session) -> None:
        session.execute(delete(AndroidTvDiscoveredHost))
        for h, p, fn, uid, model, mac in records:
            session.add(
                AndroidTvDiscoveredHost(
                    host=h,
                    port=p,
                    updated_at=now,
                    friendly_name=fn,
                    uuid=uid,
                    model_name=model,
                    mac=mac,
                )
            )

    discovery_write(path, _write)


def _nonblank_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def save_configs(
    path: Path,
    rows: Sequence[
        tuple[str, str | None, dict[str, Any], bool] | tuple[str, str | None, dict[str, Any], bool, str | None]
    ],
) -> None:
    """Replace all rows with ``(host, alias, config_dict, requires_klap_auth[, mac])``.

    When ``mac`` is known, rows are de-duplicated by MAC (later hosts with the
    same MAC win) so DHCP IP changes do not leave stale host-primary duplicates.
    """
    now = time.time()
    normalized: list[tuple[str, str | None, dict[str, Any], bool, str | None]] = []
    for row in rows:
        host, alias, cfg, requires_klap_auth = row[0], row[1], row[2], row[3]
        # Empty / whitespace MAC strings intentionally become None (same as omit).
        mac_raw = row[4] if len(row) > 4 else None
        mac = try_normalize_mac(str(mac_raw)) if mac_raw else None
        normalized.append((host, alias, cfg, requires_klap_auth, mac))

    # Prefer the last row for a given MAC so rediscovery at a new IP replaces
    # the old host without leaving two prefs identities for one device.
    by_mac: dict[str, tuple[str, str | None, dict[str, Any], bool, str | None]] = {}
    without_mac: list[tuple[str, str | None, dict[str, Any], bool, str | None]] = []
    for entry in normalized:
        mac = entry[4]
        if mac is None:
            without_mac.append(entry)
        else:
            prior = by_mac.get(mac)
            if prior is not None and prior[0] != entry[0]:
                _LOGGER.warning(
                    "Kasa discovery cache: MAC %s remapped from host %s to %s (keeping latest)",
                    mac,
                    prior[0],
                    entry[0],
                )
            by_mac[mac] = entry
    mac_hosts = {entry[0] for entry in by_mac.values()}
    deduped = list(by_mac.values()) + [e for e in without_mac if e[0] not in mac_hosts]

    def _write(session: Session) -> None:
        session.execute(delete(KasaDiscoveredDevice))
        for h, a, d, requires_klap_auth, mac in deduped:
            session.add(
                KasaDiscoveredDevice(
                    host=h,
                    alias=a,
                    config_json=json.dumps(d),
                    requires_klap_auth=1 if requires_klap_auth else 0,
                    mac=mac,
                    updated_at=now,
                )
            )

    discovery_write(path, _write)


def save_sonos_zones(
    path: Path,
    rows: Iterable[tuple[str, str, str | None] | tuple[str, str, str | None, str | None]],
) -> None:
    """Replace all rows; each entry is ``(uuid, host, zone_name | None[, mac])``.

    Empty/whitespace UUIDs and hosts are dropped (they indicate a bug in the
    discovery layer rather than a usable cache row).
    """
    now = time.time()
    records: list[tuple[str, str, str | None, str | None]] = []
    for r in rows:
        uid = str(r[0]).strip()
        host = str(r[1]).strip()
        if not uid or not host:
            continue
        name: str | None = None
        if r[2] is not None:
            stripped = str(r[2]).strip()
            name = stripped if stripped else None
        mac = try_normalize_mac(str(r[3])) if len(r) > 3 and r[3] else None
        records.append((uid, host, name, mac))

    def _write(session: Session) -> None:
        session.execute(delete(SonosKnownZone))
        for u, h, n, mac in records:
            session.add(SonosKnownZone(uuid=u, host=h, zone_name=n, mac=mac, updated_at=now))

    discovery_write(path, _write)


def save_tailwind_host(path: Path, host: str, *, mac: str | None = None) -> None:
    """Remember the last reachable Tailwind controller address (token still comes from env / CLI)."""
    now = time.time()
    mac_s = try_normalize_mac(mac) if mac else None

    def _write(session: Session) -> None:
        row = session.get(TailwindLastHost, 1)
        if row is None:
            session.add(TailwindLastHost(id=1, host=host.strip(), mac=mac_s, updated_at=now))
        else:
            row.host = host.strip()
            if mac_s is not None:
                row.mac = mac_s
            row.updated_at = now

    discovery_write(path, _write)


def load_sonos_zones(path: Path) -> list[tuple[str, str, str | None, str | None]]:
    """Return ``(uuid, host, zone_name, mac)`` rows ordered by zone_name.

    The UUID (e.g. ``RINCON_…``) is the secondary Sonos zone identifier; ``host`` is
    the last known LAN address (may drift with DHCP) and ``zone_name`` is the
    user-facing label (e.g. ``"Living Room"``). ``mac`` is preferred as the UI /
    prefs canonical key when known. Cache consumers reconnect with
    :func:`soco.SoCo` and verify ``.uid`` matches before trusting the row.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(SonosKnownZone).order_by(func.coalesce(SonosKnownZone.zone_name, SonosKnownZone.uuid))
        ).all()
        out: list[tuple[str, str, str | None, str | None]] = []
        for row in rows:
            label = (row.zone_name or "").strip() or None
            mac = (getattr(row, "mac", None) or "").strip() or None
            out.append((row.uuid, row.host, label, mac))
        return out


def load_tailwind_host(path: Path) -> str | None:
    """Return cached Tailwind host or ``None`` if missing / DB absent."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        row = session.get(TailwindLastHost, 1)
        return row.host.strip() if row else None


def load_tailwind_host_row(path: Path) -> tuple[str, str | None] | None:
    """Return ``(host, mac)`` for the cached Tailwind hub, or ``None``."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        row = session.get(TailwindLastHost, 1)
        if row is None:
            return None
        mac = (getattr(row, "mac", None) or "").strip() or None
        return row.host.strip(), mac


def migrate_canonical_key_to_mac(
    path: Path,
    *,
    backend: str,
    old_key: str,
    mac: str,
) -> None:
    """Rewrite ``ui_preferences`` + ``device_display_names`` from ``old_key`` → ``mac``.

    ``mac`` may be a plain normalized MAC or a compound key (e.g. Tailwind
    ``{hub_mac}:{door_id}``). Plain MAC strings are normalized; other forms
    are used as-is after strip.

    When both the old and MAC-keyed rows exist, keep the MAC row's flags /
    display name (prefer the already-migrated identity) and drop the old key.
    """
    old = old_key.strip()
    normalized = try_normalize_mac(mac)
    new = normalized if normalized is not None else mac.strip()
    if not old or not new or old == new:
        return
    be = backend.strip()

    def _write(session: Session) -> None:
        pref_old = session.get(UiPreference, (be, old))
        pref_new = session.get(UiPreference, (be, new))
        if pref_old is not None:
            if pref_new is None:
                session.add(
                    UiPreference(
                        backend=be,
                        canonical_key=new,
                        exclude_from_global=pref_old.exclude_from_global,
                        hide_on_mobile=pref_old.hide_on_mobile,
                        updated_at=time.time(),
                    )
                )
            session.delete(pref_old)
        disp_old = session.get(DeviceDisplayName, (be, old))
        disp_new = session.get(DeviceDisplayName, (be, new))
        if disp_old is not None:
            if disp_new is None:
                session.add(
                    DeviceDisplayName(
                        backend=be,
                        canonical_key=new,
                        display_name=disp_old.display_name,
                        updated_at=time.time(),
                    )
                )
            session.delete(disp_old)

    discovery_write(path, _write)


def find_vizio_tv_row(
    path: Path,
    device_id: str,
) -> tuple[str, int, str | None, str | None, str | None, str | None] | None:
    """Return the cached TV row matching ``device_id`` (MAC or legacy host id)."""
    needle = device_id.strip()
    if not needle:
        return None
    for row in load_vizio_tvs(path):
        host, port, display, model, mac, diid = row
        canonical = vizio_device_id_from_parts(mac=mac, host=host, port=port)
        if canonical == needle:
            return row
        if device_id_for(host, port) == needle:
            return row
        if mac and device_id_for_vizio(mac) == needle:
            return row
    if is_vizio_mac_device_id(needle):
        return None
    try:
        host, port = parse_host_spec(needle)
    except ValueError:
        return None
    if device_id_for(host, port) == needle:
        return host, port, None, None, None, None
    return None


def migrate_vizio_ui_preference_key(
    path: Path,
    *,
    old_key: str,
    new_key: str,
) -> None:
    """Move one Vizio UI preference row when the canonical device id changes."""
    old = old_key.strip()
    new = new_key.strip()
    if not old or not new or old == new:
        return

    def _write(session: Session) -> tuple[bool, bool] | None:
        row = session.get(UiPreference, ("vizio", old))
        if row is None:
            return None
        exclude = bool(row.exclude_from_global)
        hide = bool(row.hide_on_mobile)
        session.delete(row)
        return (exclude, hide)

    flags = discovery_write(path, _write)
    if flags is None:
        return
    exclude, hide = flags
    upsert_ui_preference(
        path,
        backend="vizio",
        canonical_key=new,
        exclude_from_global=exclude,
        hide_on_mobile=hide,
    )


def load_vizio_tvs(
    path: Path,
) -> list[tuple[str, int, str | None, str | None, str | None, str | None]]:
    """Return ``(host, port, display_name, model, mac, diid)`` rows ordered by host."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(select(VizioKnownTv).order_by(VizioKnownTv.host)).all()
        out: list[tuple[str, int, str | None, str | None, str | None, str | None]] = []
        for row in rows:
            display = (row.display_name or "").strip() or None
            model = (row.model or "").strip() or None
            mac = (row.mac or "").strip() or None
            diid = (row.diid or "").strip() or None
            out.append((row.host, int(row.port), display, model, mac, diid))
        return out


def load_ep1_devices(
    path: Path,
) -> list[tuple[str, int, str | None, str | None]]:
    """Return ``(host, port, mac, friendly_name)`` rows ordered by host."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(select(Ep1KnownDevice).order_by(Ep1KnownDevice.host)).all()
        out: list[tuple[str, int, str | None, str | None]] = []
        for row in rows:
            mac = (row.mac or "").strip() or None
            name = (row.friendly_name or "").strip() or None
            out.append((row.host, int(row.port), mac, name))
        return out


def load_ui_preferences(path: Path) -> list[tuple[str, str, bool, bool]]:
    """Return ``(backend, canonical_key, exclude_from_global, hide_on_mobile)`` rows.

    Backend strings mirror :func:`load_display_names`: ``kasa``, ``tailwind``,
    ``androidtv``, ``sonos``. Missing file → empty list.

    The ``exclude_from_global`` flag means a global "turn off all" / "close
    all" action must skip this device. Per-device toggles still operate on
    excluded devices; family-level bulks may also still operate on them
    (callers decide; see :mod:`app.api.app` once the tile UI lands).

    ``hide_on_mobile`` means the compact (phone / tablet) layout should omit
    the tile client-side; the device remains in ``/v1/ui/state`` and is fully
    controllable on the comfortable desktop layout. Bulk actions ignore it.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(select(UiPreference).order_by(UiPreference.backend, UiPreference.canonical_key)).all()
        return [
            (
                row.backend,
                row.canonical_key,
                bool(row.exclude_from_global),
                bool(row.hide_on_mobile),
            )
            for row in rows
        ]


def load_display_names(path: Path) -> list[tuple[str, str, str]]:
    """Rows ``(backend, canonical_key, display_name)`` — backend ``kasa``, ``tailwind``, or ``androidtv``."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(select(DeviceDisplayName)).all()
        return [(row.backend, row.canonical_key, row.display_name) for row in rows]


def upsert_display_name(
    path: Path,
    *,
    backend: str,
    canonical_key: str,
    display_name: str,
) -> None:
    now = time.time()

    def _write(session: Session) -> None:
        b = backend.strip()
        k = canonical_key.strip()
        row = session.get(DeviceDisplayName, (b, k))
        if row is None:
            session.add(
                DeviceDisplayName(
                    backend=b,
                    canonical_key=k,
                    display_name=display_name.strip(),
                    updated_at=now,
                )
            )
        else:
            row.display_name = display_name.strip()
            row.updated_at = now

    discovery_write(path, _write)


def upsert_ep1_device(
    path: Path,
    *,
    host: str,
    port: int,
    mac: str | None,
    friendly_name: str | None,
) -> None:
    """Remember one EP1 endpoint (Noise PSK lives in ``app_secrets``)."""
    now = time.time()
    h = host.strip()
    mac_s: str | None = None
    if mac:
        mac_s = try_normalize_mac(mac.strip())

    def _write(session: Session) -> None:
        preserved_label: str | None = None
        if mac_s is not None:
            existing = session.scalar(select(Ep1KnownDevice).where(Ep1KnownDevice.mac == mac_s))
            if existing is not None and existing.host != h:
                preserved_label = (existing.friendly_name or "").strip() or None
                session.delete(existing)
                session.flush()
        row = session.get(Ep1KnownDevice, h)
        label = (friendly_name or "").strip() or preserved_label
        if row is None:
            session.add(
                Ep1KnownDevice(
                    host=h,
                    port=port,
                    mac=mac_s,
                    friendly_name=label,
                    updated_at=now,
                )
            )
        else:
            row.port = port
            if label is not None:
                row.friendly_name = label
            if mac_s is not None:
                row.mac = mac_s
            row.updated_at = now

    discovery_write(path, _write)


def upsert_vizio_tv(
    path: Path,
    *,
    host: str,
    port: int,
    display_name: str | None,
    model: str | None,
    mac: str | None,
    diid: str | None,
) -> None:
    """Remember one Vizio TV endpoint (auth token lives in ``app_secrets``)."""
    now = time.time()
    h = host.strip()
    mac_s: str | None = None
    if mac:
        mac_s = try_normalize_mac(mac.strip())

    def _write(session: Session) -> None:
        preserved_diid: str | None = None
        preserved_label: str | None = None
        preserved_model: str | None = None
        if mac_s is not None:
            existing = session.scalar(select(VizioKnownTv).where(VizioKnownTv.mac == mac_s))
            if existing is not None and existing.host != h:
                # Same MAC at a new IP: drop the stale host PK row, but keep
                # metadata when this call does not re-supply it.
                preserved_diid = (existing.diid or "").strip() or None
                preserved_label = (existing.display_name or "").strip() or None
                preserved_model = (existing.model or "").strip() or None
                session.delete(existing)
                session.flush()
        row = session.get(VizioKnownTv, h)
        label = (display_name or "").strip() or preserved_label
        model_s = (model or "").strip() or preserved_model
        diid_s = (diid or "").strip() or preserved_diid
        if row is None:
            session.add(
                VizioKnownTv(
                    host=h,
                    port=port,
                    display_name=label,
                    model=model_s,
                    mac=mac_s,
                    diid=diid_s,
                    updated_at=now,
                )
            )
        else:
            row.port = port
            if label is not None:
                row.display_name = label
            if model_s is not None:
                row.model = model_s
            if mac_s is not None:
                row.mac = mac_s
            if diid_s is not None:
                row.diid = diid_s
            row.updated_at = now

    discovery_write(path, _write)
    if mac_s is not None:
        canonical = vizio_device_id_from_parts(mac=mac_s, host=h, port=port)
        for old in {device_id_for(h, port), h}:
            if old != canonical:
                migrate_vizio_ui_preference_key(path, old_key=old, new_key=canonical)


def upsert_ui_preference(
    path: Path,
    *,
    backend: str,
    canonical_key: str,
    exclude_from_global: bool,
    hide_on_mobile: bool,
) -> None:
    """Insert or replace one ``(backend, canonical_key)`` UI preference row.

    Stored as ``INTEGER 0/1`` because SQLite has no native bool. The
    :func:`load_ui_preferences` reader converts back to :class:`bool`.
    """
    now = time.time()

    def _write(session: Session) -> None:
        b = backend.strip()
        k = canonical_key.strip()
        row = session.get(UiPreference, (b, k))
        if row is None:
            session.add(
                UiPreference(
                    backend=b,
                    canonical_key=k,
                    exclude_from_global=1 if exclude_from_global else 0,
                    hide_on_mobile=1 if hide_on_mobile else 0,
                    updated_at=now,
                )
            )
        else:
            row.exclude_from_global = 1 if exclude_from_global else 0
            row.hide_on_mobile = 1 if hide_on_mobile else 0
            row.updated_at = now

    discovery_write(path, _write)


def delete_display_name(path: Path, *, backend: str, canonical_key: str) -> None:
    def _write(session: Session) -> None:
        row = session.get(DeviceDisplayName, (backend.strip(), canonical_key.strip()))
        if row is not None:
            session.delete(row)

    discovery_write(path, _write)


def delete_ui_preference(path: Path, *, backend: str, canonical_key: str) -> None:
    """Forget a per-device UI preference row.

    Equivalent to a tile reverting to defaults (``exclude_from_global=False``,
    ``hide_on_mobile=False``). Used by future tile-management endpoints; not
    exercised by the current landing page.
    """

    def _write(session: Session) -> None:
        row = session.get(UiPreference, (backend.strip(), canonical_key.strip()))
        if row is not None:
            session.delete(row)

    discovery_write(path, _write)
