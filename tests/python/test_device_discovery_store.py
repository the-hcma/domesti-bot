"""Tests for :mod:`device_discovery_store` (no hardware)."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path

from app import device_discovery_store
from app.kasa_device_manager import KasaDeviceManager


def test_ensure_schema_upgrades_legacy_database(tmp_path: Path) -> None:
    """Older files had only ``kasa_discovered_devices``; reads must create newer tables."""

    db = tmp_path / "legacy.sqlite"
    with contextlib.closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE kasa_discovered_devices (host TEXT PRIMARY KEY, config_json TEXT)")
        conn.commit()

    device_discovery_store.ensure_schema(db)
    with contextlib.closing(sqlite3.connect(db)) as conn:
        cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
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
    assert device_discovery_store.load_cached_configs(db) == []


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
    device_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Desk lamp", cfg, False)],
    )
    rows = device_discovery_store.load_cached_configs(db)
    assert rows == [("192.168.1.50", "Desk lamp", cfg, False, None)]

    with contextlib.closing(device_discovery_store.open_db(db)) as conn:
        cur = conn.execute("SELECT host, alias, config_json FROM kasa_discovered_devices")
        h, alias, raw = cur.fetchone()
        assert h == "192.168.1.50"
        assert alias == "Desk lamp"
        assert json.loads(raw) == cfg


def test_save_replaces_previous_rows(tmp_path: Path) -> None:
    db = tmp_path / "kasa.sqlite"
    device_discovery_store.save_configs(
        db,
        [
            ("10.0.0.1", "a", {"host": "10.0.0.1"}, False),
            ("10.0.0.2", "b", {"host": "10.0.0.2"}, True),
        ],
    )
    device_discovery_store.save_configs(
        db,
        [("10.0.0.3", "c", {"host": "10.0.0.3"}, True)],
    )
    rows = device_discovery_store.load_cached_configs(db)
    assert rows == [("10.0.0.3", "c", {"host": "10.0.0.3"}, True, None)]


def test_save_configs_dedupes_same_mac_different_hosts(tmp_path: Path) -> None:
    db = tmp_path / "kasa.sqlite"
    cfg_a = {"host": "10.0.0.1"}
    cfg_b = {"host": "10.0.0.2"}
    device_discovery_store.save_configs(
        db,
        [
            ("10.0.0.1", "Lamp", cfg_a, False, "aa:bb:cc:dd:ee:ff"),
            ("10.0.0.2", "Lamp", cfg_b, False, "aa:bb:cc:dd:ee:ff"),
        ],
    )
    rows = device_discovery_store.load_cached_configs(db)
    assert len(rows) == 1
    assert rows[0][0] == "10.0.0.2"
    assert rows[0][4] == "aa:bb:cc:dd:ee:ff"


def test_migrate_canonical_key_to_mac_moves_prefs_and_display(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="10.0.0.1",
        exclude_from_global=True,
        hide_on_mobile=False,
    )
    device_discovery_store.upsert_display_name(
        db,
        backend="kasa",
        canonical_key="10.0.0.1",
        display_name="Desk",
    )
    device_discovery_store.migrate_canonical_key_to_mac(
        db,
        backend="kasa",
        old_key="10.0.0.1",
        mac="aa:bb:cc:dd:ee:ff",
    )
    prefs = device_discovery_store.load_ui_preferences(db)
    assert prefs == [("kasa", "aa:bb:cc:dd:ee:ff", True, False)]
    names = device_discovery_store.load_display_names(db)
    assert names == [("kasa", "aa:bb:cc:dd:ee:ff", "Desk")]


def test_migrate_canonical_key_prefers_existing_mac_row(tmp_path: Path) -> None:
    db = tmp_path / "prefs.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="10.0.0.1",
        exclude_from_global=True,
        hide_on_mobile=True,
    )
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="aa:bb:cc:dd:ee:ff",
        exclude_from_global=False,
        hide_on_mobile=False,
    )
    device_discovery_store.migrate_canonical_key_to_mac(
        db,
        backend="kasa",
        old_key="10.0.0.1",
        mac="aa:bb:cc:dd:ee:ff",
    )
    prefs = device_discovery_store.load_ui_preferences(db)
    assert prefs == [("kasa", "aa:bb:cc:dd:ee:ff", False, False)]


def test_display_names_upsert_load_delete(tmp_path: Path) -> None:
    db = tmp_path / "d.sqlite"
    device_discovery_store.upsert_display_name(
        db, backend="kasa", canonical_key="192.168.1.2", display_name="Desk light"
    )
    rows = device_discovery_store.load_display_names(db)
    assert rows == [("kasa", "192.168.1.2", "Desk light")]
    device_discovery_store.delete_display_name(db, backend="kasa", canonical_key="192.168.1.2")
    assert device_discovery_store.load_display_names(db) == []


def test_androidtv_hosts_roundtrip_with_friendly_name(tmp_path: Path) -> None:
    db = tmp_path / "atv.sqlite"
    device_discovery_store.save_androidtv_hosts(
        db,
        [
            ("192.168.1.10", 5555, "Living room"),
            ("192.168.1.20", 5555, None),
        ],
    )
    rows = device_discovery_store.load_androidtv_endpoint_rows(db)
    assert rows == [
        ("192.168.1.10", 5555, "Living room"),
        ("192.168.1.20", 5555, None),
    ]
    assert device_discovery_store.load_androidtv_hosts(db) == [
        ("192.168.1.10", 5555),
        ("192.168.1.20", 5555),
    ]


def test_androidtv_known_devices_roundtrip_with_uuid_and_model(tmp_path: Path) -> None:
    """Saving the 5-/6-tuple shape must round-trip (mac optional)."""

    db = tmp_path / "atv.sqlite"
    device_discovery_store.save_androidtv_hosts(
        db,
        [
            ("192.168.1.10", 8009, "Living room", "uuid-aaa", "Chromecast"),
            ("192.168.1.20", 8009, "Kitchen", "uuid-bbb", "Nest Audio", "aa:bb:cc:dd:ee:ff"),
        ],
    )
    assert device_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", "uuid-aaa", "Chromecast", None),
        ("192.168.1.20", 8009, "Kitchen", "uuid-bbb", "Nest Audio", "aa:bb:cc:dd:ee:ff"),
    ]
    # The narrower endpoint API must still see the friendly_name column.
    assert device_discovery_store.load_androidtv_endpoint_rows(db) == [
        ("192.168.1.10", 8009, "Living room"),
        ("192.168.1.20", 8009, "Kitchen"),
    ]


def test_androidtv_known_devices_back_compat_when_uuid_missing(tmp_path: Path) -> None:
    """3-tuple writes must surface as rows with ``uuid IS NULL`` / ``model IS NULL``."""

    db = tmp_path / "atv.sqlite"
    device_discovery_store.save_androidtv_hosts(
        db,
        [("192.168.1.10", 8009, "Living room")],
    )
    assert device_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", None, None, None),
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
            "INSERT INTO androidtv_discovered_hosts (host, port, updated_at, friendly_name) VALUES (?, ?, ?, ?)",
            ("192.168.1.10", 8009, 0.0, "Living room"),
        )
        conn.commit()

    device_discovery_store.ensure_schema(db)

    with contextlib.closing(sqlite3.connect(db)) as conn:
        cur = conn.execute("PRAGMA table_info(androidtv_discovered_hosts)")
        cols = {row[1] for row in cur.fetchall()}
        assert {"host", "port", "updated_at", "friendly_name", "uuid", "model_name", "mac"} <= cols
    # Existing row reads back with NULLs for the new columns.
    assert device_discovery_store.load_androidtv_known_devices(db) == [
        ("192.168.1.10", 8009, "Living room", None, None, None),
    ]


def test_tailwind_host_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "mix.sqlite"
    assert device_discovery_store.load_tailwind_host(db) is None
    device_discovery_store.save_tailwind_host(db, "192.168.86.42")
    assert device_discovery_store.load_tailwind_host(db) == "192.168.86.42"
    device_discovery_store.save_tailwind_host(db, "10.0.0.7")
    assert device_discovery_store.load_tailwind_host(db) == "10.0.0.7"


def test_manager_constructor_accepts_cache_path(tmp_path: Path) -> None:
    db = tmp_path / "k.sqlite"
    kdm = KasaDeviceManager(discovery_cache_path=db, force_discovery=True)
    assert kdm._discovery_cache_path == db.expanduser().resolve()
    assert kdm._force_discovery is True


def test_sonos_zones_load_missing_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "missing.sqlite"
    assert device_discovery_store.load_sonos_zones(db) == []


def test_sonos_zones_roundtrip(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_AAA", "192.168.1.10", "Living Room"),
            ("RINCON_BBB", "192.168.1.11", "Kitchen"),
            ("RINCON_CCC", "192.168.1.12", None),
        ],
    )
    rows = device_discovery_store.load_sonos_zones(db)
    # Default order is by ``COALESCE(zone_name, uuid)`` ascending — None-named
    # zones sort by UUID and end up alongside their lettered peers.
    assert rows == [
        ("RINCON_BBB", "192.168.1.11", "Kitchen", None),
        ("RINCON_AAA", "192.168.1.10", "Living Room", None),
        ("RINCON_CCC", "192.168.1.12", None, None),
    ]


def test_sonos_zones_save_replaces_previous_rows(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "10.0.0.1", "A"),
            ("RINCON_B", "10.0.0.2", "B"),
        ],
    )
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_C", "10.0.0.3", "C")],
    )
    rows = device_discovery_store.load_sonos_zones(db)
    assert rows == [("RINCON_C", "10.0.0.3", "C", None)]


def test_sonos_zones_save_drops_empty_uuid_or_host(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_OK", "192.168.1.10", "Den"),
            ("", "192.168.1.99", "blank uuid"),
            ("RINCON_BAD", "   ", "blank host"),
            ("  RINCON_TRIM  ", "  192.168.1.20  ", "  Trim me  "),
        ],
    )
    rows = device_discovery_store.load_sonos_zones(db)
    assert rows == [
        ("RINCON_OK", "192.168.1.10", "Den", None),
        ("RINCON_TRIM", "192.168.1.20", "Trim me", None),
    ]


def test_ui_preferences_delete_removes_row(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="192.168.1.50",
        exclude_from_global=True,
        hide_on_mobile=False,
    )
    assert device_discovery_store.load_ui_preferences(db) == [
        ("kasa", "192.168.1.50", True, False),
    ]
    device_discovery_store.delete_ui_preference(db, backend="kasa", canonical_key="192.168.1.50")
    assert device_discovery_store.load_ui_preferences(db) == []


def test_ui_preferences_distinct_backends_dont_collide(tmp_path: Path) -> None:
    """``(backend, canonical_key)`` is the composite PK; same key per backend coexists."""

    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="left",
        exclude_from_global=True,
        hide_on_mobile=False,
    )
    device_discovery_store.upsert_ui_preference(
        db,
        backend="tailwind",
        canonical_key="left",
        exclude_from_global=False,
        hide_on_mobile=True,
    )
    rows = device_discovery_store.load_ui_preferences(db)
    assert rows == [
        ("kasa", "left", True, False),
        ("tailwind", "left", False, True),
    ]


def test_ui_preferences_hide_on_mobile_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="k1",
        exclude_from_global=False,
        hide_on_mobile=True,
    )
    rows = device_discovery_store.load_ui_preferences(db)
    assert rows == [("kasa", "k1", False, True)]
    assert type(rows[0][3]) is bool


def test_migrate_vizio_ui_preference_key_preserves_both_flags(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="vizio",
        canonical_key="192.168.1.10",
        exclude_from_global=True,
        hide_on_mobile=True,
    )
    device_discovery_store.migrate_vizio_ui_preference_key(
        db,
        old_key="192.168.1.10",
        new_key="aa:bb:cc:dd:ee:ff",
    )
    assert device_discovery_store.load_ui_preferences(db) == [
        ("vizio", "aa:bb:cc:dd:ee:ff", True, True),
    ]


def test_ui_preferences_load_missing_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "absent.sqlite"
    assert device_discovery_store.load_ui_preferences(db) == []


def test_ui_preferences_load_returns_python_bool_not_int(tmp_path: Path) -> None:
    """Loader must coerce SQLite's ``INTEGER`` (0/1) to :class:`bool`.

    Avoids subtle bugs in JSON serialization (Pydantic + bool checks) where
    integer values would slip through and surface in API payloads.
    """

    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="k1",
        exclude_from_global=True,
        hide_on_mobile=True,
    )
    rows = device_discovery_store.load_ui_preferences(db)
    assert rows == [("kasa", "k1", True, True)]
    # ``isinstance(True, int)`` is True in Python (bool subclasses int), so
    # ``isinstance(..., bool)`` alone isn't enough; assert the exact type.
    assert type(rows[0][2]) is bool
    assert type(rows[0][3]) is bool


def test_ui_preferences_orders_by_backend_then_key(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    for backend, key in [
        ("tailwind", "garage-2"),
        ("kasa", "192.168.1.50"),
        ("kasa", "192.168.1.10"),
        ("tailwind", "garage-1"),
    ]:
        device_discovery_store.upsert_ui_preference(
            db,
            backend=backend,
            canonical_key=key,
            exclude_from_global=False,
            hide_on_mobile=False,
        )
    rows = device_discovery_store.load_ui_preferences(db)
    assert rows == [
        ("kasa", "192.168.1.10", False, False),
        ("kasa", "192.168.1.50", False, False),
        ("tailwind", "garage-1", False, False),
        ("tailwind", "garage-2", False, False),
    ]


def test_ui_preferences_upsert_overwrites_previous_value(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="k",
        exclude_from_global=True,
        hide_on_mobile=True,
    )
    assert device_discovery_store.load_ui_preferences(db) == [
        ("kasa", "k", True, True),
    ]
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="k",
        exclude_from_global=False,
        hide_on_mobile=False,
    )
    assert device_discovery_store.load_ui_preferences(db) == [
        ("kasa", "k", False, False),
    ]


def test_ui_preferences_upsert_load_round_trip_default_false(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="192.168.1.42",
        exclude_from_global=False,
        hide_on_mobile=False,
    )
    rows = device_discovery_store.load_ui_preferences(db)
    assert rows == [("kasa", "192.168.1.42", False, False)]


def test_upsert_vizio_tv_preserves_metadata_when_mac_moves_host(tmp_path: Path) -> None:
    """DHCP remap by MAC must not drop display_name / model / diid when omitted."""

    db = tmp_path / "vizio.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.1.10",
        port=7345,
        display_name="Kitchen TV",
        model="V505",
        mac="00:bd:3e:d5:f0:11",
        diid="diid-1",
    )
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.1.99",
        port=7345,
        display_name=None,
        model=None,
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    rows = device_discovery_store.load_vizio_tvs(db)
    assert len(rows) == 1
    host, port, display, model, mac, diid = rows[0]
    assert host == "192.168.1.99"
    assert port == 7345
    assert display == "Kitchen TV"
    assert model == "V505"
    assert mac == "00:bd:3e:d5:f0:11"
    assert diid == "diid-1"
