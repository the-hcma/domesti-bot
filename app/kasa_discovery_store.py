"""SQLite persistence for LAN device discovery (Kasa configs, Tailwind host, Google Cast endpoints).

Schema changes are **additive** only: ``CREATE TABLE IF NOT EXISTS`` applied via
:func:`open_db` and :func:`ensure_schema` (no Alembic-style data migrations).
Reads call :func:`ensure_schema` so older on-disk files gain new tables automatically.

The ``androidtv_discovered_hosts`` table name is historical: rows store **Cast** ``(host, port,
friendly_name)`` hints from PyChromecast (typically port **8009**), not ADB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kasa_discovered_devices (
    host TEXT PRIMARY KEY NOT NULL,
    alias TEXT,
    config_json TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tailwind_last_host (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    host TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS device_display_names (
    backend TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (backend, canonical_key)
);
CREATE TABLE IF NOT EXISTS androidtv_discovered_hosts (
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    updated_at REAL NOT NULL,
    friendly_name TEXT,
    uuid TEXT,
    model_name TEXT,
    PRIMARY KEY (host, port)
);
CREATE TABLE IF NOT EXISTS sonos_known_zones (
    uuid TEXT PRIMARY KEY NOT NULL,
    host TEXT NOT NULL,
    zone_name TEXT,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS ui_preferences (
    backend TEXT NOT NULL,
    canonical_key TEXT NOT NULL,
    exclude_from_global INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (backend, canonical_key)
);
"""


def _apply_androidtv_friendly_name_migration(conn: sqlite3.Connection) -> None:
    """Add ``friendly_name`` to legacy DBs created before that column existed."""

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='androidtv_discovered_hosts'"
    )
    if cur.fetchone() is None:
        return
    cur = conn.execute("PRAGMA table_info(androidtv_discovered_hosts)")
    cols = {row[1] for row in cur.fetchall()}
    if "friendly_name" in cols:
        return
    conn.execute("ALTER TABLE androidtv_discovered_hosts ADD COLUMN friendly_name TEXT")


def _apply_androidtv_uuid_model_migration(conn: sqlite3.Connection) -> None:
    """Add ``uuid`` + ``model_name`` columns so the Cast cache-hit path can
    skip mDNS entirely (call ``pychromecast.get_chromecast_from_host`` with
    the cached UUID instead of running a zeroconf browse).

    Pre-migration rows will have ``uuid IS NULL`` and the manager falls back
    to the mDNS path until they're rewritten by the next successful sweep.
    """

    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='androidtv_discovered_hosts'"
    )
    if cur.fetchone() is None:
        return
    cur = conn.execute("PRAGMA table_info(androidtv_discovered_hosts)")
    cols = {row[1] for row in cur.fetchall()}
    if "uuid" not in cols:
        conn.execute("ALTER TABLE androidtv_discovered_hosts ADD COLUMN uuid TEXT")
    if "model_name" not in cols:
        conn.execute("ALTER TABLE androidtv_discovered_hosts ADD COLUMN model_name TEXT")


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the cache database and ensure the schema exists."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
    _apply_androidtv_friendly_name_migration(conn)
    _apply_androidtv_uuid_model_migration(conn)
    conn.commit()
    return conn


def ensure_schema(path: Path) -> None:
    """Apply ``CREATE TABLE IF NOT EXISTS`` to an existing file so older DBs gain new tables.

    There is no separate migration runner; additive schema is idempotent via ``IF NOT EXISTS``.
    """

    path = path.expanduser().resolve()
    if not path.is_file():
        return
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_SCHEMA)
        _apply_androidtv_friendly_name_migration(conn)
        _apply_androidtv_uuid_model_migration(conn)
        conn.commit()
    finally:
        conn.close()


def load_androidtv_endpoint_rows(path: Path) -> list[tuple[str, int, str | None]]:
    """Return ``(host, port, friendly_name)`` rows ordered by host, port. Missing file → empty."""

    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        _apply_androidtv_friendly_name_migration(conn)
        conn.commit()
        cur = conn.execute(
            "SELECT host, port, friendly_name FROM androidtv_discovered_hosts "
            "ORDER BY host, port"
        )
        out: list[tuple[str, int, str | None]] = []
        for h, p, fn in cur.fetchall():
            label = (str(fn).strip() if fn is not None else "") or None
            out.append((str(h), int(p), label))
        return out
    finally:
        conn.close()


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
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        _apply_androidtv_friendly_name_migration(conn)
        _apply_androidtv_uuid_model_migration(conn)
        conn.commit()
        cur = conn.execute(
            "SELECT host, port, friendly_name, uuid, model_name "
            "FROM androidtv_discovered_hosts ORDER BY host, port"
        )
        out: list[tuple[str, int, str | None, str | None, str | None]] = []
        for h, p, fn, uid, model in cur.fetchall():
            label = (str(fn).strip() if fn is not None else "") or None
            uid_s = (str(uid).strip() if uid is not None else "") or None
            model_s = (str(model).strip() if model is not None else "") or None
            out.append((str(h), int(p), label, uid_s, model_s))
        return out
    finally:
        conn.close()


def load_cached_configs(path: Path) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(host, config_dict)`` rows ordered by host. Missing file → empty list."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT host, config_json FROM kasa_discovered_devices ORDER BY host"
        )
        return [(host, json.loads(raw)) for host, raw in cur.fetchall()]
    finally:
        conn.close()


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

    conn = open_db(path)
    try:
        conn.execute("DELETE FROM androidtv_discovered_hosts")
        now = time.time()
        records: list[tuple[str, int, str | None, str | None, str | None]] = []
        for r in rows:
            # Treat the row as a positional sequence to keep the call-site
            # tuple-shape flexible without spawning a pyright complaint per
            # optional column. ``r[3]`` / ``r[4]`` only fire when the
            # caller actually provided that many positional fields.
            seq: tuple[Any, ...] = tuple(r)
            h = str(seq[0]).strip()
            p = int(seq[1])
            fn = _nonblank_str(seq[2]) if len(seq) > 2 else None
            uid = _nonblank_str(seq[3]) if len(seq) > 3 else None
            model = _nonblank_str(seq[4]) if len(seq) > 4 else None
            records.append((h, p, fn, uid, model))
        conn.executemany(
            "INSERT INTO androidtv_discovered_hosts "
            "(host, port, updated_at, friendly_name, uuid, model_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [(h, p, now, fn, uid, model) for h, p, fn, uid, model in records],
        )
        conn.commit()
    finally:
        conn.close()


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
    conn = open_db(path)
    try:
        conn.execute("DELETE FROM kasa_discovered_devices")
        now = time.time()
        conn.executemany(
            "INSERT INTO kasa_discovered_devices (host, alias, config_json, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [(h, a, json.dumps(d), now) for h, a, d in rows],
        )
        conn.commit()
    finally:
        conn.close()


def save_sonos_zones(
    path: Path,
    rows: Iterable[tuple[str, str, str | None]],
) -> None:
    """Replace all rows; each entry is ``(uuid, host, zone_name | None)``.

    Empty/whitespace UUIDs and hosts are dropped (they indicate a bug in the
    discovery layer rather than a usable cache row).
    """

    conn = open_db(path)
    try:
        conn.execute("DELETE FROM sonos_known_zones")
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
        conn.executemany(
            "INSERT INTO sonos_known_zones (uuid, host, zone_name, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [(u, h, n, now) for u, h, n in triples],
        )
        conn.commit()
    finally:
        conn.close()


def save_tailwind_host(path: Path, host: str) -> None:
    """Remember the last reachable Tailwind controller address (token still comes from env / CLI)."""
    conn = open_db(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO tailwind_last_host (id, host, updated_at) "
            "VALUES (1, ?, ?)",
            (host.strip(), time.time()),
        )
        conn.commit()
    finally:
        conn.close()


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
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT uuid, host, zone_name FROM sonos_known_zones "
            "ORDER BY COALESCE(zone_name, uuid)"
        )
        rows: list[tuple[str, str, str | None]] = []
        for u, h, n in cur.fetchall():
            label = (str(n).strip() if n is not None else "") or None
            rows.append((str(u), str(h), label))
        return rows
    finally:
        conn.close()


def load_tailwind_host(path: Path) -> str | None:
    """Return cached Tailwind host or ``None`` if missing / DB absent."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return None
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute("SELECT host FROM tailwind_last_host WHERE id = 1")
        row = cur.fetchone()
        return str(row[0]).strip() if row else None
    finally:
        conn.close()


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
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT backend, canonical_key, exclude_from_global "
            "FROM ui_preferences ORDER BY backend, canonical_key"
        )
        return [(str(b), str(k), bool(int(x))) for b, k, x in cur.fetchall()]
    finally:
        conn.close()


def load_display_names(path: Path) -> list[tuple[str, str, str]]:
    """Rows ``(backend, canonical_key, display_name)`` — backend ``kasa``, ``tailwind``, or ``androidtv``."""
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    ensure_schema(path)
    conn = sqlite3.connect(path)
    try:
        cur = conn.execute(
            "SELECT backend, canonical_key, display_name FROM device_display_names"
        )
        return [(str(b), str(k), str(n)) for b, k, n in cur.fetchall()]
    finally:
        conn.close()


def upsert_display_name(
    path: Path,
    *,
    backend: str,
    canonical_key: str,
    display_name: str,
) -> None:
    conn = open_db(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO device_display_names "
            "(backend, canonical_key, display_name, updated_at) VALUES (?, ?, ?, ?)",
            (
                backend.strip(),
                canonical_key.strip(),
                display_name.strip(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


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

    conn = open_db(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO ui_preferences "
            "(backend, canonical_key, exclude_from_global, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (
                backend.strip(),
                canonical_key.strip(),
                1 if exclude_from_global else 0,
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def delete_display_name(path: Path, *, backend: str, canonical_key: str) -> None:
    conn = open_db(path)
    try:
        conn.execute(
            "DELETE FROM device_display_names WHERE backend = ? AND canonical_key = ?",
            (backend.strip(), canonical_key.strip()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_ui_preference(path: Path, *, backend: str, canonical_key: str) -> None:
    """Forget a per-device UI preference row.

    Equivalent to a tile reverting to defaults (``exclude_from_global=False``).
    Used by future tile-management endpoints; not exercised by the current
    landing page.
    """

    conn = open_db(path)
    try:
        conn.execute(
            "DELETE FROM ui_preferences WHERE backend = ? AND canonical_key = ?",
            (backend.strip(), canonical_key.strip()),
        )
        conn.commit()
    finally:
        conn.close()
