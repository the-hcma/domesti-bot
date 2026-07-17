"""Kasa MAC-primary identity and prefs migration (hermetic)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app import device_discovery_store
from app.kasa_device_manager import KasaDevice, _make_kasa_device


def test_kasa_device_host_property() -> None:
    kdev = MagicMock()
    kdev.host = "10.0.0.5"
    kdev.is_on = False
    kd = KasaDevice("aa:bb:cc:dd:ee:ff", kdev, mac_address="aa:bb:cc:dd:ee:ff")
    assert kd.host == "10.0.0.5"
    assert kd.identifier == "aa:bb:cc:dd:ee:ff"


def test_make_kasa_device_falls_back_to_arp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.kasa_device_manager.lookup_mac_via_arp",
        lambda host: "11:22:33:44:55:66",
    )
    dev = MagicMock()
    dev.host = "10.0.0.2"
    dev.alias = "Plug"
    dev.mac = None
    dev.sys_info = {}
    kd = _make_kasa_device(dev)
    assert kd is not None
    assert kd.identifier == "11:22:33:44:55:66"


def test_make_kasa_device_prefers_cached_mac_over_arp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.kasa_device_manager.lookup_mac_via_arp",
        lambda host: "11:22:33:44:55:66",
    )
    dev = MagicMock()
    dev.host = "10.0.0.3"
    dev.alias = "Plug"
    dev.mac = None
    dev.sys_info = {}
    kd = _make_kasa_device(dev, cached_mac="aa:bb:cc:dd:ee:ff")
    assert kd is not None
    assert kd.identifier == "aa:bb:cc:dd:ee:ff"
    assert kd.mac_address == "aa:bb:cc:dd:ee:ff"


def test_make_kasa_device_prefers_vendor_mac() -> None:
    dev = MagicMock()
    dev.host = "10.0.0.1"
    dev.alias = "Lamp"
    dev.mac = "AA:BB:CC:DD:EE:FF"
    dev.sys_info = {}
    kd = _make_kasa_device(dev)
    assert kd is not None
    assert kd.identifier == "aa:bb:cc:dd:ee:ff"
    assert kd.mac_address == "aa:bb:cc:dd:ee:ff"
    assert kd.preferred_label == "Lamp"


def test_same_mac_new_ip_migrates_prefs(tmp_path: Path) -> None:
    db = tmp_path / "kasa.sqlite"
    cfg_a: dict[str, Any] = {"host": "10.0.0.1"}
    cfg_b: dict[str, Any] = {"host": "10.0.0.99"}
    device_discovery_store.save_configs(
        db,
        [("10.0.0.1", "Lamp", cfg_a, False, "aa:bb:cc:dd:ee:ff")],
    )
    device_discovery_store.upsert_ui_preference(
        db,
        backend="kasa",
        canonical_key="10.0.0.1",
        exclude_from_global=True,
        hide_on_mobile=False,
    )
    device_discovery_store.migrate_canonical_key_to_mac(
        db,
        backend="kasa",
        old_key="10.0.0.1",
        mac="aa:bb:cc:dd:ee:ff",
    )
    device_discovery_store.save_configs(
        db,
        [("10.0.0.99", "Lamp", cfg_b, False, "aa:bb:cc:dd:ee:ff")],
    )
    rows = device_discovery_store.load_cached_configs(db)
    assert len(rows) == 1
    assert rows[0][0] == "10.0.0.99"
    assert rows[0][4] == "aa:bb:cc:dd:ee:ff"
    prefs = device_discovery_store.load_ui_preferences(db)
    assert prefs == [("kasa", "aa:bb:cc:dd:ee:ff", True, False)]
