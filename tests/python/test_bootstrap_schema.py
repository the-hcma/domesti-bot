"""Tests for discovery database bootstrap idempotency."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from app.db.engine import dispose_engine
from app.db.schema import bootstrap_schema, clear_bootstrap_cache


def test_bootstrap_schema_concurrent_calls_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    _legacy_mytracks_settings_db(db)
    clear_bootstrap_cache()

    def _bootstrap() -> None:
        bootstrap_schema(db)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_bootstrap) for _ in range(24)]
        for future in as_completed(futures):
            future.result()

    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(mytracks_settings)")}
    assert "paired_at" in cols
    dispose_engine(db)


def test_bootstrap_schema_syncs_missing_mytracks_columns(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    _legacy_mytracks_settings_db(db)
    clear_bootstrap_cache()

    bootstrap_schema(db)

    with sqlite3.connect(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(mytracks_settings)")}
    assert "paired_at" in cols
    assert "location_updates_accepted" in cols
    assert "user_location_update_url" in cols
    dispose_engine(db)


def test_bootstrap_schema_upgrades_legacy_kasa_database(tmp_path: Path) -> None:
    db = tmp_path / "legacy.sqlite"
    _legacy_kasa_only_db(db)
    clear_bootstrap_cache()

    bootstrap_schema(db)

    with sqlite3.connect(db) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        kasa_cols = {row[1] for row in conn.execute("PRAGMA table_info(kasa_discovered_devices)")}
    assert "device_display_names" in names
    assert "smtp_settings" in names
    assert "alias" in kasa_cols
    assert "updated_at" in kasa_cols
    dispose_engine(db)


def _legacy_kasa_only_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE kasa_discovered_devices (host TEXT PRIMARY KEY, config_json TEXT)")


def _legacy_mytracks_settings_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE mytracks_settings ("
            "id INTEGER PRIMARY KEY CHECK (id = 1), "
            "domain TEXT NOT NULL, "
            "updated_at REAL NOT NULL, "
            "username TEXT NOT NULL DEFAULT ''"
            ")"
        )
        conn.execute("INSERT INTO mytracks_settings (id, domain, updated_at) VALUES (1, 'x', 0)")
