"""CSS / source contract for toast and Settings status success/failure colors."""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_SETTINGS_STATUS_TS = _REPO_ROOT / "web" / "src" / "settings-status.ts"
_UI_TOAST_TS = _REPO_ROOT / "web" / "src" / "ui-toast.ts"


def test_action_toast_variants_use_explicit_tone_classes() -> None:
    css = _INDEX_HTML.read_text(encoding="utf-8")
    assert ".action-toast-error" in css
    assert ".action-toast-success" in css
    assert ".action-toast-info" in css
    # Danger red must not live only on bare .action-toast (fragile default).
    assert "action-toast-error { border-color: var(--danger)" in css
    toast_ts = _UI_TOAST_TS.read_text(encoding="utf-8")
    assert 'return "action-toast action-toast-error"' in toast_ts
    assert 'return "action-toast action-toast-success"' in toast_ts
    assert 'return "action-toast action-toast-info"' in toast_ts


def test_settings_dialog_status_tones_are_colored() -> None:
    css = _INDEX_HTML.read_text(encoding="utf-8")
    assert ".settings-dialog-status-error { color: var(--danger); }" in css
    assert ".settings-dialog-status-success { color: var(--accent); }" in css
    assert ".settings-dialog-status-info { color: var(--muted); }" in css
    status_ts = _SETTINGS_STATUS_TS.read_text(encoding="utf-8")
    assert "settings-dialog-status-error" in status_ts
    assert "settings-dialog-status-success" in status_ts
    assert "setSettingsDialogStatus" in status_ts
