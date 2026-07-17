from __future__ import annotations

from pathlib import Path

import pytest


def _hermetic_arp_mac(host: str) -> str:
    """Deterministic MAC derived from an IPv4 host for hermetic discovery tests."""
    parts = str(host).split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        a, b, c, d = (int(part) for part in parts)
        return f"aa:bb:{a:02x}:{b:02x}:{c:02x}:{d:02x}"
    return "aa:bb:cc:dd:ee:ff"


@pytest.fixture(autouse=True)
def _set_default_automation_rules_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default tests to the committed example rules bundle."""
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))


@pytest.fixture(autouse=True)
def _stub_lookup_mac_via_arp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supply ARP MAC addresses so discovery can require them without a neighbor table.

    Individual tests may still override ``lookup_mac_via_arp`` (e.g. to assert a
    miss). Patches every manager import site that calls the helper.
    """

    for target in (
        "app.device_mac.lookup_mac_via_arp",
        "app.kasa_device_manager.lookup_mac_via_arp",
        "app.sonos_device_manager.lookup_mac_via_arp",
        "app.gotailwind_device_manager.lookup_mac_via_arp",
        "app.androidtv_device_manager.lookup_mac_via_arp",
        "app.vizio_device_manager.lookup_mac_via_arp",
        "app.vizio_mac.lookup_mac_via_arp",
    ):
        monkeypatch.setattr(target, _hermetic_arp_mac)
