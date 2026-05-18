"""CSS contract for tablet-sized settings/about dialogs."""

from __future__ import annotations

from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parents[2] / "app" / "api" / "static" / "index.html"


def test_index_html_enlarges_about_dialog_on_tablet_viewport() -> None:
    css = _INDEX_HTML.read_text(encoding="utf-8")
    assert "@media (min-width: 769px) and (max-width: 1100px)" in css
    assert ".about-dialog-meta { font-size: 16px" in css
    assert ".about-dialog-repo { font-size: 18px" in css
