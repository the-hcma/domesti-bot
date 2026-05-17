"""Tests for ``app.compact_icon_svg_process``."""

from __future__ import annotations

import re
from pathlib import Path

from app.compact_icon_svg_process import process_compact_icon_svg_bytes

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "compact_icons"


def test_process_compact_icon_svg_strips_bulb_label_and_background() -> None:
    raw = (_FIXTURES / "bulb_raw.svg").read_bytes()
    out = process_compact_icon_svg_bytes(raw).decode("utf-8")
    assert "clip-path" not in out
    assert 'fill="currentColor"' in out
    assert re.search(r"<path\b", out)
    assert "M582 254" not in out
    assert "M618 333" not in out
    assert "M520 764" not in out
    assert "M651 589" not in out
    assert "m-421 -388" in out


def test_process_compact_icon_svg_strips_garage_bottom_labels() -> None:
    raw = (_FIXTURES / "garage_closed_raw.svg").read_bytes()
    out = process_compact_icon_svg_bytes(raw).decode("utf-8")
    assert "M272 264" not in out
    assert "M430 265" not in out
    assert "M530 794" in out
