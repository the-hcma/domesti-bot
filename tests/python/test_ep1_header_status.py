"""CSS + compact/comfortable visibility for the EP1 header status strip (#524)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EP1_HEADER_TS = _REPO_ROOT / "web" / "src" / "ep1-header-status.ts"
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_MAIN_TS = _REPO_ROOT / "web" / "src" / "main.ts"


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.browser
def test_ep1_header_status_compact_stacks_readings_leaves_bulk_width(
    chromium_browser: Any,
) -> None:
    """Phone: stacked readings (no label); bulk-off keeps horizontal room."""
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app" data-layout="compact">
  <header class="tile-header tile-header-global">
    <span class="brand-mark">
      <svg class="brand-mark-svg" width="32" height="32" viewBox="0 0 24 24"></svg>
    </span>
    <aside class="ep1-header-status" data-mock="true" aria-label="Room sensors">
      <div class="ep1-header-status-device" title="Office EP1">
        <span class="ep1-header-status-metric" data-metric="temperature">
          <span class="ep1-header-status-full">22.5 °C / 72.5 °F</span>
          <span class="ep1-header-status-compact">22.5°C/72.5°F</span>
        </span>
        <span class="ep1-header-status-metric" data-metric="humidity">
          <span class="ep1-header-status-full">42%</span>
          <span class="ep1-header-status-compact">42%</span>
        </span>
        <span class="ep1-header-status-metric" data-metric="illuminance">
          <span class="ep1-header-status-full">180 lx</span>
          <span class="ep1-header-status-compact">180 lx</span>
        </span>
      </div>
    </aside>
    <div class="tile-header-actions">
      <button type="button" class="btn btn-bulk tile-header-global-off">
        Turn off / pause / close everything
      </button>
    </div>
  </header>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(html)
        assert page.locator(".ep1-header-status-label").count() == 0
        assert page.locator('[data-metric="occupancy"]').count() == 0
        assert "Office EP1" not in page.locator(".ep1-header-status").inner_text()

        temp = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-compact',
        )
        humidity = page.locator(
            '.ep1-header-status-metric[data-metric="humidity"] .ep1-header-status-compact',
        )
        lux = page.locator(
            '.ep1-header-status-metric[data-metric="illuminance"] .ep1-header-status-compact',
        )
        assert temp.evaluate("el => getComputedStyle(el).display") == "inline"
        assert temp.inner_text() == "22.5°C/72.5°F"

        temp_box = temp.bounding_box()
        humidity_box = humidity.bounding_box()
        lux_box = lux.bounding_box()
        assert temp_box is not None
        assert humidity_box is not None
        assert lux_box is not None
        assert temp_box["y"] < humidity_box["y"] < lux_box["y"]

        brand_box = page.locator(".brand-mark").bounding_box()
        strip_box = page.locator(".ep1-header-status").bounding_box()
        bulk_box = page.locator(".tile-header-global-off").bounding_box()
        assert brand_box is not None
        assert strip_box is not None
        assert bulk_box is not None
        # Narrow column beside the icon; bulk button keeps most of the row width.
        assert strip_box["width"] < 120
        assert bulk_box["width"] > 180
        assert strip_box["x"] + strip_box["width"] <= bulk_box["x"] + 2
    finally:
        page.close()


def test_ep1_header_status_module_readings_only_contract() -> None:
    src = _EP1_HEADER_TS.read_text(encoding="utf-8")
    assert "export interface Ep1HeaderStatusSnapshot" in src
    assert "export const MOCK_EP1_HEADER_STATUS" in src
    assert "export function createEp1HeaderStatusStrip" in src
    assert "TODO(ep1-header-live)" in src
    assert "ep1-header-status-label" not in src
    assert "formatEp1HeaderOccupancy" not in src
    assert 'data-metric="occupancy"' not in src
    assert "temperature_c" in src
    assert "temperature_f" in src
    assert "humidity_pct" in src
    assert "illuminance_lx" in src
    assert "°C/" in src
    assert "°F" in src
    assert "row.title = snapshot.label" in src


def test_index_html_ep1_header_status_css_contract() -> None:
    style = _extract_index_html_style_block()
    base = _css_rule_block(style, ".ep1-header-status")
    assert "display: flex" in base
    assert "flex: 0 1 auto" in base
    assert "ep1-header-status-label" not in style
    assert "data-occupancy" not in style
    header = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header.tile-header-global',
    )
    assert "flex-wrap: nowrap" in header
    compact = _css_rule_block(style, '#app[data-layout="compact"] .ep1-header-status')
    assert "flex: 0 0 auto" in compact
    assert "order: 3" not in compact
    device = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-device',
    )
    assert "flex-direction: column" in device
    separators = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-metric ~ .ep1-header-status-metric::before',
    )
    assert "content: none" in separators


def test_main_appends_ep1_header_strip_and_skips_ep1_family_tiles() -> None:
    src = _MAIN_TS.read_text(encoding="utf-8")
    assert "appendEp1HeaderStatusStrip(header)" in src
    assert "createEp1HeaderStatusStrip(MOCK_EP1_HEADER_STATUS" in src
    assert 'family.id === "ep1"' in src
    assert 'from "./ep1-header-status.js"' in src


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
