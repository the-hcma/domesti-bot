"""Tests for :mod:`kasa_discovery_store` (no hardware)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import kasa_discovery_store


def test_ensure_schema_upgrades_legacy_database(tmp_path: Path) -> None:
    """Older files had only ``kasa_discovered_devices``; reads must create newer tables."""

    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE kasa_discovered_devices (host TEXT PRIMARY KEY, config_json TEXT)"
    )
    conn.commit()
    conn.close()

    kasa_discovery_store.ensure_schema(db)
    conn = sqlite3.connect(db)
    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
        assert "device_display_names" in names
        assert "tailwind_last_host" in names
        assert "kasa_discovered_devices" in names
    finally:
        conn.close()


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "none.sqlite"
    assert kasa_discovery_store.load_cached_configs(db) == []


def test_roundtrip_save_and_load(tmp_path: Path) -> None:
    db = tmp_path / "kasa.sqlite"
    cfg = {
        "host": "192.168.1.50",
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }
    kasa_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Desk lamp", cfg)],
    )
    rows = kasa_discovery_store.load_cached_configs(db)
    assert rows == [("192.168.1.50", cfg)]

    conn = kasa_discovery_store.open_db(db)
    try:
        cur = conn.execute(
            "SELECT host, alias, config_json FROM kasa_discovered_devices"
        )
        h, alias, raw = cur.fetchone()
        assert h == "192.168.1.50"
        assert alias == "Desk lamp"
        assert json.loads(raw) == cfg
    finally:
        conn.close()


def test_save_replaces_previous_rows(tmp_path: Path) -> None:
    db = tmp_path / "kasa.sqlite"
    kasa_discovery_store.save_configs(
        db,
        [
            ("10.0.0.1", "a", {"host": "10.0.0.1"}),
            ("10.0.0.2", "b", {"host": "10.0.0.2"}),
        ],
    )
    kasa_discovery_store.save_configs(
        db,
        [("10.0.0.3", "c", {"host": "10.0.0.3"})],
    )
    rows = kasa_discovery_store.load_cached_configs(db)
    assert rows == [("10.0.0.3", {"host": "10.0.0.3"})]


def test_display_names_upsert_load_delete(tmp_path: Path) -> None:
    db = tmp_path / "d.sqlite"
    kasa_discovery_store.upsert_display_name(
        db, backend="kasa", canonical_key="192.168.1.2", display_name="Desk light"
    )
    rows = kasa_discovery_store.load_display_names(db)
    assert rows == [("kasa", "192.168.1.2", "Desk light")]
    kasa_discovery_store.delete_display_name(db, backend="kasa", canonical_key="192.168.1.2")
    assert kasa_discovery_store.load_display_names(db) == []


def test_tailwind_host_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "mix.sqlite"
    assert kasa_discovery_store.load_tailwind_host(db) is None
    kasa_discovery_store.save_tailwind_host(db, "192.168.86.42")
    assert kasa_discovery_store.load_tailwind_host(db) == "192.168.86.42"
    kasa_discovery_store.save_tailwind_host(db, "10.0.0.7")
    assert kasa_discovery_store.load_tailwind_host(db) == "10.0.0.7"


def test_manager_constructor_accepts_cache_path(tmp_path: Path) -> None:
    from kasa_device_manager import KasaDeviceManager

    db = tmp_path / "k.sqlite"
    kdm = KasaDeviceManager(discovery_cache_path=db, force_discovery=True)
    assert kdm._discovery_cache_path == db.expanduser().resolve()
    assert kdm._force_discovery is True
