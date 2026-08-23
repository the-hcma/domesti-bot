"""Hermetic contract for Settings → Device Discovery UI copy and CSS (#684)."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_PANEL_TS = _REPO_ROOT / "web" / "src" / "discovery-settings-panel.ts"


def test_discovery_settings_source_labels_locked_for_gotailwind() -> None:
    text = _PANEL_TS.read_text(encoding="utf-8")
    assert 'familyId === "gotailwind" ? "cached hub" : "cache"' in text
    assert 'familyId === "gotailwind" ? "LAN hub lookup" : "LAN discovery"' in text
    assert "export function formatDiscoverySourceLabel" in text


def test_discovery_settings_new_device_badge_contract() -> None:
    panel = _PANEL_TS.read_text(encoding="utf-8")
    css = _INDEX_HTML.read_text(encoding="utf-8")
    assert "discovery-settings-device-new" in panel
    assert "DISCOVERY_NEW_DEVICE_BADGE" in panel
    assert "discovery-settings-device-new" in css
    assert "discovery-settings-device-new-badge" in css
    assert "action-toast-clock-icon" in css
    assert "discovery-progress-bar" in css
