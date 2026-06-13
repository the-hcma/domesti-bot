"""Compact icon resolution for the Vizio family."""

from __future__ import annotations

from pathlib import Path

from app.ui_compact_icon import resolve_compact_icon

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPACT_ICONS_DIR = _REPO_ROOT / "app" / "api" / "static" / "icons" / "compact"


def test_resolve_compact_icon_returns_tv_for_vizio_family() -> None:
    assert (
        resolve_compact_icon(
            family_id="vizio",
            label="Kitchen TV",
            kind="switch",
        )
        == "tv"
    )


def test_vizio_tv_state_icons_exist_on_disk() -> None:
    present = {path.stem for path in _COMPACT_ICONS_DIR.glob("*.svg")}
    assert "tv_on" in present
    assert "tv_off" in present
