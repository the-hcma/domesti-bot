"""Tests for :mod:`kasa_discovery_store` (no hardware)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

import pytest

from app import kasa_discovery_store
from app.kasa_device_manager import KasaDeviceManager


def test_ensure_schema_upgrades_legacy_database(tmp_path: Path) -> None:
    """Older files had only ``kasa_discovered_devices``; reads must create newer tables."""

    db = tmp_path / "legacy.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute(
            "CREATE TABLE kasa_discovered_devices (host TEXT PRIMARY KEY, config_json TEXT)"
        )
        conn.commit()

    kasa_discovery_store.ensure_schema(db)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        names = {row[0] for row in cur.fetchall()}
        assert "device_display_names" in names
        assert "tailwind_last_host" in names
        assert "kasa_discovered_devices" in names
        assert "sonos_known_zones" in names
        assert "app_secrets" in names
        assert "smtp_settings" in names
        assert "ui_preferences" in names


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

    with contextlib.closing(kasa_discovery_store.open_db(db)) as conn:
        cur = conn.execute(
            "SELECT host, alias, config_json FROM kasa_discovered_devices"
        )
        h, alias, raw = cur.fetchone()
        assert h == "192.168.1.50"
        assert alias == "Desk lamp"
        assert json.loads(raw) == cfg


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
    with contextlib.closing(sqlite3.connect(db)) as conn:
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

    kasa_discovery_store.ensure_schema(db)

    with contextlib.closing(sqlite3.connect(db)) as conn:
        cur = conn.execute("PRAGMA table_info(androidtv_discovered_hosts)")
        cols = {row[1] for row in cur.fetchall()}
        assert {"host", "port", "updated_at", "friendly_name", "uuid", "model_name"} <= cols
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


def test_ui_preferences_delete_removes_row(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="192.168.1.50", exclude_from_global=True
    )
    assert kasa_discovery_store.load_ui_preferences(db) == [
        ("kasa", "192.168.1.50", True),
    ]
    kasa_discovery_store.delete_ui_preference(
        db, backend="kasa", canonical_key="192.168.1.50"
    )
    assert kasa_discovery_store.load_ui_preferences(db) == []


def test_ui_preferences_distinct_backends_dont_collide(tmp_path: Path) -> None:
    """``(backend, canonical_key)`` is the composite PK; same key per backend coexists."""

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="left", exclude_from_global=True
    )
    kasa_discovery_store.upsert_ui_preference(
        db, backend="tailwind", canonical_key="left", exclude_from_global=False
    )
    rows = kasa_discovery_store.load_ui_preferences(db)
    assert rows == [
        ("kasa", "left", True),
        ("tailwind", "left", False),
    ]


def test_ui_preferences_load_missing_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "absent.sqlite"
    assert kasa_discovery_store.load_ui_preferences(db) == []


def test_ui_preferences_load_returns_python_bool_not_int(tmp_path: Path) -> None:
    """Loader must coerce SQLite's ``INTEGER`` (0/1) to :class:`bool`.

    Avoids subtle bugs in JSON serialization (Pydantic + bool checks) where
    integer values would slip through and surface in API payloads.
    """

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="k1", exclude_from_global=True
    )
    rows = kasa_discovery_store.load_ui_preferences(db)
    assert rows == [("kasa", "k1", True)]
    # ``isinstance(True, int)`` is True in Python (bool subclasses int), so
    # ``isinstance(..., bool)`` alone isn't enough; assert the exact type.
    assert type(rows[0][2]) is bool


def test_ui_preferences_orders_by_backend_then_key(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    for backend, key in [
        ("tailwind", "garage-2"),
        ("kasa", "192.168.1.50"),
        ("kasa", "192.168.1.10"),
        ("tailwind", "garage-1"),
    ]:
        kasa_discovery_store.upsert_ui_preference(
            db, backend=backend, canonical_key=key, exclude_from_global=False
        )
    rows = kasa_discovery_store.load_ui_preferences(db)
    assert rows == [
        ("kasa", "192.168.1.10", False),
        ("kasa", "192.168.1.50", False),
        ("tailwind", "garage-1", False),
        ("tailwind", "garage-2", False),
    ]


def test_ui_preferences_upsert_overwrites_previous_value(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="k", exclude_from_global=True
    )
    assert kasa_discovery_store.load_ui_preferences(db) == [("kasa", "k", True)]
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="k", exclude_from_global=False
    )
    assert kasa_discovery_store.load_ui_preferences(db) == [("kasa", "k", False)]


def test_ui_preferences_upsert_load_round_trip_default_false(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="192.168.1.42",
        exclude_from_global=False,
    )
    rows = kasa_discovery_store.load_ui_preferences(db)
    assert rows == [("kasa", "192.168.1.42", False)]
