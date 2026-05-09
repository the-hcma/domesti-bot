"""SQLite persistence for LAN device discovery (Kasa configs + Tailwind controller host).

Schema changes are **additive** only: ``CREATE TABLE IF NOT EXISTS`` applied via
:func:`open_db` and :func:`ensure_schema` (no Alembic-style data migrations).
Reads call :func:`ensure_schema` so older on-disk files gain new tables automatically.
"""

from __future__ import annotations

import json
import sqlite3
import time
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
"""


def open_db(path: Path) -> sqlite3.Connection:
    """Open or create the cache database and ensure the schema exists."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(_SCHEMA)
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
        conn.commit()
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


def load_display_names(path: Path) -> list[tuple[str, str, str]]:
    """Rows ``(backend, canonical_key, display_name)`` where backend is ``kasa`` or ``tailwind``."""
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
