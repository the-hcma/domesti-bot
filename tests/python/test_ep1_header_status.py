"""CSS + compact/comfortable visibility for the EP1 header status strip (#524)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.browser
def test_ep1_header_status_compact_uses_short_occupancy_labels(
    chromium_browser: Any,
) -> None:
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app" data-layout="compact">
  <header class="tile-header tile-header-global">
    <aside class="ep1-header-status" data-mock="true" aria-label="Room sensors">
      <div class="ep1-header-status-device" data-occupancy="occupied">
        <span class="ep1-header-status-label">Office EP1</span>
        <span class="ep1-header-status-metric" data-metric="occupancy">
          <span class="ep1-header-status-full">Occupied</span>
          <span class="ep1-header-status-compact">Occ</span>
        </span>
        <span class="ep1-header-status-metric" data-metric="temperature">
          <span class="ep1-header-status-full">22.5 °C / 72.5 °F</span>
          <span class="ep1-header-status-compact">22.5 °C</span>
        </span>
      </div>
    </aside>
    <div class="tile-header-actions">
      <button type="button" class="btn btn-bulk tile-header-global-off">Turn off</button>
    </div>
  </header>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(html)
        occ_full = page.locator(
            '.ep1-header-status-metric[data-metric="occupancy"] .ep1-header-status-full',
        )
        occ_compact = page.locator(
            '.ep1-header-status-metric[data-metric="occupancy"] .ep1-header-status-compact',
        )
        assert occ_full.evaluate("el => getComputedStyle(el).display") == "none"
        assert occ_compact.evaluate("el => getComputedStyle(el).display") == "inline"
        assert occ_compact.inner_text() == "Occ"
    finally:
        page.close()


def test_index_html_ep1_header_status_css_contract() -> None:
    style = _extract_index_html_style_block()
    base = _css_rule_block(style, ".ep1-header-status")
    assert "display: flex" in base
    compact = _css_rule_block(style, '#app[data-layout="compact"] .ep1-header-status')
    assert "flex: 1 1 100%" in compact
    assert "order: 3" in compact
    full_hidden = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-full',
    )
    assert "display: none" in full_hidden
    compact_shown = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-compact',
    )
    assert "display: inline" in compact_shown


def _css_rule_block(style_css: str, selector_needle: str) -> str:
    idx = style_css.find(selector_needle)
    assert idx >= 0, f"Expected rule {selector_needle!r} in index.html <style>"
    brace = style_css.find("{", idx)
    assert brace > idx
    depth = 0
    for i, ch in enumerate(style_css[brace:], start=brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return style_css[idx : i + 1]
    raise AssertionError(f"Unclosed CSS rule for {selector_needle!r}")


def _extract_index_html_style_block() -> str:
    raw = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    match = re.search(r"<style>(.*?)</style>", raw, flags=re.DOTALL)
    assert match is not None, "Expected <style> block in index.html"
    return match.group(1)
