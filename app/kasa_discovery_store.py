"""SQLite persistence for LAN device discovery (Kasa configs, Tailwind host, Google Cast endpoints).

Schema changes are **additive** only: tables are created via SQLAlchemy
:func:`app.db.schema.ensure_schema` (no Alembic-style data migrations).
Reads call :func:`ensure_schema` so older on-disk files gain new tables automatically.

The ``androidtv_discovered_hosts`` table name is historical: rows store **Cast** ``(host, port,
friendly_name)`` hints from PyChromecast (typically port **8009**), not ADB.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.db.models import (
    AndroidTvDiscoveredHost,
    DeviceDisplayName,
    KasaDiscoveredDevice,
    SonosKnownZone,
    TailwindLastHost,
    UiPreference,
)
from app.db.schema import bootstrap_schema, ensure_schema_if_exists
from app.db.session import discovery_session


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
            select(AndroidTvDiscoveredHost).order_by(
                AndroidTvDiscoveredHost.host, AndroidTvDiscoveredHost.port
            )
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
) -> list[tuple[str, int, str | None, str | None, str | None]]:
    """Return rows ``(host, port, friendly_name, uuid, model_name)`` ordered by host, port.

    Rows that pre-date the uuid/model_name migration will have ``uuid IS NULL``
    (and possibly ``model_name IS NULL``). The Cast manager treats a non-empty
    ``uuid`` on **every** row as the trigger for the no-mDNS fast path; a
    single missing/empty UUID falls back to the targeted-mDNS path that
    rewrites the cache.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(AndroidTvDiscoveredHost).order_by(
                AndroidTvDiscoveredHost.host, AndroidTvDiscoveredHost.port
            )
        ).all()
        out: list[tuple[str, int, str | None, str | None, str | None]] = []
        for row in rows:
            label = (row.friendly_name or "").strip() or None
            uid_s = (row.uuid or "").strip() or None
            model_s = (row.model_name or "").strip() or None
            out.append((row.host, int(row.port), label, uid_s, model_s))
        return out


def load_cached_configs(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(host, config_dict)`` rows ordered by host. Missing file → empty list."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(KasaDiscoveredDevice).order_by(KasaDiscoveredDevice.host)
        ).all()
        return [(row.host, json.loads(row.config_json)) for row in rows]


def save_androidtv_hosts(
    path: Path,
    rows: Iterable[
        tuple[str, int]
        | tuple[str, int, str | None]
        | tuple[str, int, str | None, str | None]
        | tuple[str, int, str | None, str | None, str | None]
    ],
) -> None:
    """Replace all rows.

    Accepted shapes (additive, backward-compatible):
      * ``(host, port)``
      * ``(host, port, friendly_name)``
      * ``(host, port, friendly_name, uuid)``
      * ``(host, port, friendly_name, uuid, model_name)``

    Missing trailing values are persisted as ``NULL``. The Cast cache-fast
    path requires every row's ``uuid`` to be non-empty, so callers that want
    the no-mDNS startup must pass the 4- or 5-tuple shape.
    """
    now = time.time()
    records: list[tuple[str, int, str | None, str | None, str | None]] = []
    for r in rows:
        seq: tuple[Any, ...] = tuple(r)
        h = str(seq[0]).strip()
        p = int(seq[1])
        fn = _nonblank_str(seq[2]) if len(seq) > 2 else None
        uid = _nonblank_str(seq[3]) if len(seq) > 3 else None
        model = _nonblank_str(seq[4]) if len(seq) > 4 else None
        records.append((h, p, fn, uid, model))
    with discovery_session(path) as session:
        session.execute(delete(AndroidTvDiscoveredHost))
        for h, p, fn, uid, model in records:
            session.add(
                AndroidTvDiscoveredHost(
                    host=h,
                    port=p,
                    updated_at=now,
                    friendly_name=fn,
                    uuid=uid,
                    model_name=model,
                )
            )


def _nonblank_str(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def save_configs(
    path: Path,
    rows: list[tuple[str, str | None, dict[str, Any]]],
) -> None:
    """Replace all rows with ``(host, alias, config_dict)`` snapshots."""
    now = time.time()
    with discovery_session(path) as session:
        session.execute(delete(KasaDiscoveredDevice))
        for h, a, d in rows:
            session.add(
                KasaDiscoveredDevice(
                    host=h,
                    alias=a,
                    config_json=json.dumps(d),
                    updated_at=now,
                )
            )


def save_sonos_zones(
    path: Path,
    rows: Iterable[tuple[str, str, str | None]],
) -> None:
    """Replace all rows; each entry is ``(uuid, host, zone_name | None)``.

    Empty/whitespace UUIDs and hosts are dropped (they indicate a bug in the
    discovery layer rather than a usable cache row).
    """
    now = time.time()
    triples: list[tuple[str, str, str | None]] = []
    for r in rows:
        uid = str(r[0]).strip()
        host = str(r[1]).strip()
        if not uid or not host:
            continue
        name: str | None = None
        if r[2] is not None:
            stripped = str(r[2]).strip()
            name = stripped if stripped else None
        triples.append((uid, host, name))
    with discovery_session(path) as session:
        session.execute(delete(SonosKnownZone))
        for u, h, n in triples:
            session.add(
                SonosKnownZone(uuid=u, host=h, zone_name=n, updated_at=now)
            )


def save_tailwind_host(path: Path, host: str) -> None:
    """Remember the last reachable Tailwind controller address (token still comes from env / CLI)."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(TailwindLastHost, 1)
        if row is None:
            session.add(TailwindLastHost(id=1, host=host.strip(), updated_at=now))
        else:
            row.host = host.strip()
            row.updated_at = now


def load_sonos_zones(path: Path) -> list[tuple[str, str, str | None]]:
    """Return ``(uuid, host, zone_name)`` rows ordered by zone_name. Missing file → empty.

    The UUID (e.g. ``RINCON_…``) is the stable Sonos zone identifier; ``host`` is
    the last known LAN address (may drift with DHCP) and ``zone_name`` is the
    user-facing label (e.g. ``"Living Room"``). Cache consumers reconnect with
    :func:`soco.SoCo` and verify ``.uid`` matches before trusting the row.
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(SonosKnownZone).order_by(
                func.coalesce(SonosKnownZone.zone_name, SonosKnownZone.uuid)
            )
        ).all()
        out: list[tuple[str, str, str | None]] = []
        for row in rows:
            label = (row.zone_name or "").strip() or None
            out.append((row.uuid, row.host, label))
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


def load_ui_preferences(path: Path) -> list[tuple[str, str, bool]]:
    """Return ``(backend, canonical_key, exclude_from_global)`` rows.

    Backend strings mirror :func:`load_display_names`: ``kasa``, ``tailwind``,
    ``androidtv``, ``sonos``. Missing file → empty list.

    The ``exclude_from_global`` flag means a global "turn off all" / "close
    all" action must skip this device. Per-device toggles still operate on
    excluded devices; family-level bulks may also still operate on them
    (callers decide; see :mod:`app.api.app` once the tile UI lands).
    """
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema_if_exists(path)
    with discovery_session(path) as session:
        rows = session.scalars(
            select(UiPreference).order_by(
                UiPreference.backend, UiPreference.canonical_key
            )
        ).all()
        return [(row.backend, row.canonical_key, bool(row.exclude_from_global)) for row in rows]


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
    with discovery_session(path) as session:
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


def upsert_ui_preference(
    path: Path,
    *,
    backend: str,
    canonical_key: str,
    exclude_from_global: bool,
) -> None:
    """Insert or replace one ``(backend, canonical_key)`` UI preference row.

    Stored as ``INTEGER 0/1`` because SQLite has no native bool. The
    :func:`load_ui_preferences` reader converts back to :class:`bool`.
    """
    now = time.time()
    with discovery_session(path) as session:
        b = backend.strip()
        k = canonical_key.strip()
        row = session.get(UiPreference, (b, k))
        if row is None:
            session.add(
                UiPreference(
                    backend=b,
                    canonical_key=k,
                    exclude_from_global=1 if exclude_from_global else 0,
                    updated_at=now,
                )
            )
        else:
            row.exclude_from_global = 1 if exclude_from_global else 0
            row.updated_at = now


def delete_display_name(path: Path, *, backend: str, canonical_key: str) -> None:
    with discovery_session(path) as session:
        row = session.get(
            DeviceDisplayName, (backend.strip(), canonical_key.strip())
        )
        if row is not None:
            session.delete(row)


def delete_ui_preference(path: Path, *, backend: str, canonical_key: str) -> None:
    """Forget a per-device UI preference row.

    Equivalent to a tile reverting to defaults (``exclude_from_global=False``).
    Used by future tile-management endpoints; not exercised by the current
    landing page.
    """
    with discovery_session(path) as session:
        row = session.get(UiPreference, (backend.strip(), canonical_key.strip()))
        if row is not None:
            session.delete(row)
