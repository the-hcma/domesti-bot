"""Static compact-tile icon assets under app/api/static/icons/compact/."""

from __future__ import annotations

import subprocess
from pathlib import Path

from app.ui_compact_icon import resolve_compact_icon

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPACT_ICONS_DIR = _REPO_ROOT / "app" / "api" / "static" / "icons" / "compact"
_PREVIEW_SCRIPT = _REPO_ROOT / "scripts" / "internal" / "generate-compact-icon-preview"
_REVIEW_FILENAME = "review.html"

_REQUIRED_KEYS = frozenset(
    {
        "bulb",
        "desk",
        "fan",
        "garage_closed",
        "garage_open",
        "lamp",
        "led",
        "lantern",
        "light",
        "outlet",
        "pendant",
        "plug",
        "speaker",
        "strip",
        "table",
        "room_attic",
        "room_basement",
        "room_bathroom",
        "room_bedroom",
        "room_dining",
        "room_garage",
        "room_guest",
        "room_hall",
        "room_kitchen",
        "room_laundry",
        "room_living",
        "room_office",
        "room_porch",
    }
)


def test_compact_icon_assets_cover_required_keys() -> None:
    present = {path.stem for path in _COMPACT_ICONS_DIR.glob("*.svg")}
    missing = sorted(_REQUIRED_KEYS - present)
    assert not missing, f"Missing compact icon files: {', '.join(missing)}"


def test_compact_icon_assets_exist_for_mock_kasa_labels() -> None:
    present = {path.stem for path in _COMPACT_ICONS_DIR.glob("*.svg")}
    for label in (
        "Kitchen",
        "Porch",
        "Office",
        "Hall",
        "Guest",
        "Basement",
        "Basement lamp",
    ):
        key = resolve_compact_icon(family_id="kasa", label=label, kind="switch")
        assert key in present, f"Expected /static/icons/compact/{key}.svg for {label!r}"


def test_generate_compact_icon_preview_writes_gallery(tmp_path: Path) -> None:
    (tmp_path / "bulb.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"/>',
        encoding="utf-8",
    )
    (tmp_path / "room_kitchen.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"/>',
        encoding="utf-8",
    )
    proc = subprocess.run(
        [
            str(_PREVIEW_SCRIPT),
            "--icon-dir",
            str(tmp_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    dest = tmp_path / _REVIEW_FILENAME
    assert dest.is_file()
    html = dest.read_text(encoding="utf-8")
    assert "review-inline-icon" in html
    assert "<img " not in html
    assert 'data-icon-key="bulb"' in html
    assert 'data-icon-key="room_kitchen"' in html
    assert "Object icons" in html
    assert "Room icons" in html
