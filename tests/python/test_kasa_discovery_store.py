"""Tests for :mod:`kasa_discovery_store` (no hardware)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import kasa_discovery_store
from app.kasa_device_manager import KasaDeviceManager


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
        assert "sonos_known_zones" in names
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


def test_androidtv_hosts_roundtrip_with_friendly_name(tmp_path: Path) -> None:
    db = tmp_path / "atv.sqlite"
    kasa_discovery_store.save_androidtv_hosts(
        db,
        [
            ("192.168.1.10", 5555, "Living room"),
            ("192.168.1.20", 5555, None),
        ],
    )
    rows = kasa_discovery_store.load_androidtv_endpoint_rows(db)
    assert rows == [
        ("192.168.1.10", 5555, "Living room"),
        ("192.168.1.20", 5555, None),
    ]
    assert kasa_discovery_store.load_androidtv_hosts(db) == [
        ("192.168.1.10", 5555),
        ("192.168.1.20", 5555),
    ]


def test_androidtv_known_devices_roundtrip_with_uuid_and_model(tmp_path: Path) -> None:
    """Saving the 5-tuple shape (host, port, friendly, uuid, model) must round-trip."""

    db = tmp_path / "atv.sqlite"
    kasa_discovery_store.save_androidtv_hosts(
        db,
        [
            ("192.168.1.10", 8009, "Living room", "uuid-aaa", "Chromecast"),
            ("192.168.1.20", 8009, "Kitchen", "uuid-bbb", "Nest Audio"),
        ],
    )
    assert kasa_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", "uuid-aaa", "Chromecast"),
        ("192.168.1.20", 8009, "Kitchen", "uuid-bbb", "Nest Audio"),
    ]
    # The narrower endpoint API must still see the friendly_name column.
    assert kasa_discovery_store.load_androidtv_endpoint_rows(db) == [
        ("192.168.1.10", 8009, "Living room"),
        ("192.168.1.20", 8009, "Kitchen"),
    ]


def test_androidtv_known_devices_back_compat_when_uuid_missing(tmp_path: Path) -> None:
    """3-tuple writes must surface as rows with ``uuid IS NULL`` / ``model IS NULL``."""

    db = tmp_path / "atv.sqlite"
    kasa_discovery_store.save_androidtv_hosts(
        db,
        [("192.168.1.10", 8009, "Living room")],
    )
    assert kasa_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", None, None),
    ]


def test_ensure_schema_adds_androidtv_uuid_and_model_columns(tmp_path: Path) -> None:
    """Legacy DBs (no uuid/model_name columns) must gain them via ALTER TABLE."""

    db = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE androidtv_discovered_hosts ("
        " host TEXT NOT NULL, port INTEGER NOT NULL, updated_at REAL NOT NULL,"
        " friendly_name TEXT, PRIMARY KEY (host, port))"
    )
    conn.execute(
        "INSERT INTO androidtv_discovered_hosts (host, port, updated_at, friendly_name) "
        "VALUES (?, ?, ?, ?)",
        ("192.168.1.10", 8009, 0.0, "Living room"),
    )
    conn.commit()
    conn.close()

    kasa_discovery_store.ensure_schema(db)

    conn = sqlite3.connect(db)
    try:
        cur = conn.execute("PRAGMA table_info(androidtv_discovered_hosts)")
        cols = {row[1] for row in cur.fetchall()}
        assert {"host", "port", "updated_at", "friendly_name", "uuid", "model_name"} <= cols
    finally:
        conn.close()
    # Existing row reads back with NULLs for the new columns.
    assert kasa_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", None, None),
    ]


def test_tailwind_host_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "mix.sqlite"
    assert kasa_discovery_store.load_tailwind_host(db) is None
    kasa_discovery_store.save_tailwind_host(db, "192.168.86.42")
    assert kasa_discovery_store.load_tailwind_host(db) == "192.168.86.42"
    kasa_discovery_store.save_tailwind_host(db, "10.0.0.7")
    assert kasa_discovery_store.load_tailwind_host(db) == "10.0.0.7"


def test_manager_constructor_accepts_cache_path(tmp_path: Path) -> None:
    db = tmp_path / "k.sqlite"
    kdm = KasaDeviceManager(discovery_cache_path=db, force_discovery=True)
    assert kdm._discovery_cache_path == db.expanduser().resolve()
    assert kdm._force_discovery is True


def test_sonos_zones_load_missing_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "missing.sqlite"
    assert kasa_discovery_store.load_sonos_zones(db) == []


def test_sonos_zones_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_AAA", "192.168.1.10", "Living Room"),
            ("RINCON_BBB", "192.168.1.11", "Kitchen"),
            ("RINCON_CCC", "192.168.1.12", None),
        ],
    )
    rows = kasa_discovery_store.load_sonos_zones(db)
    # Default order is by ``COALESCE(zone_name, uuid)`` ascending — None-named
    # zones sort by UUID and end up alongside their lettered peers.
    assert rows == [
        ("RINCON_BBB", "192.168.1.11", "Kitchen"),
        ("RINCON_AAA", "192.168.1.10", "Living Room"),
        ("RINCON_CCC", "192.168.1.12", None),
    ]


def test_sonos_zones_save_replaces_previous_rows(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "10.0.0.1", "A"),
            ("RINCON_B", "10.0.0.2", "B"),
        ],
    )
    kasa_discovery_store.save_sonos_zones(
        db,
        [("RINCON_C", "10.0.0.3", "C")],
    )
    rows = kasa_discovery_store.load_sonos_zones(db)
    assert rows == [("RINCON_C", "10.0.0.3", "C")]


def test_sonos_zones_save_drops_empty_uuid_or_host(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_OK", "192.168.1.10", "Den"),
            ("", "192.168.1.99", "blank uuid"),
            ("RINCON_BAD", "   ", "blank host"),
            ("  RINCON_TRIM  ", "  192.168.1.20  ", "  Trim me  "),
        ],
    )
    rows = kasa_discovery_store.load_sonos_zones(db)
    assert rows == [
        ("RINCON_OK", "192.168.1.10", "Den"),
        ("RINCON_TRIM", "192.168.1.20", "Trim me"),
    ]
