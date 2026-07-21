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
def test_ep1_header_status_compact_fits_brand_height_with_2x2_grid(
    chromium_browser: Any,
) -> None:
    """Phone: °C above °F in a 2×2 grid; strip height ≤ brand icon (32px)."""
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
          <span class="ep1-header-status-compact">22.5°C</span>
        </span>
        <span class="ep1-header-status-metric ep1-header-status-metric-compact-only"
              data-metric="temperature-f">
          <span class="ep1-header-status-full"></span>
          <span class="ep1-header-status-compact">72.5°F</span>
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
      <button type="button"
              class="btn btn-bulk tile-header-global-off tile-header-global-off-icons"
              aria-label="Turn off / pause / close everything">
        <span class="tile-header-global-off-glyph tile-header-global-off-off"
              aria-hidden="true">OFF</span>
        <span class="tile-header-global-off-glyph" aria-hidden="true">||</span>
        <span class="tile-header-global-off-glyph" aria-hidden="true">🔒</span>
        <span class="tile-header-global-off-glyph tile-header-global-off-all"
              aria-hidden="true">all</span>
      </button>
    </div>
  </header>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(html)
        brand_box = page.locator(".brand-mark-svg").bounding_box()
        header_box = page.locator(".tile-header.tile-header-global").bounding_box()
        strip_box = page.locator(".ep1-header-status").bounding_box()
        bulk_box = page.locator(".tile-header-global-off").bounding_box()
        assert brand_box is not None
        assert header_box is not None
        assert strip_box is not None
        assert bulk_box is not None
        # Strip must not exceed the brand icon (pre-readings vertical budget).
        assert strip_box["height"] <= brand_box["height"] + 1
        # Header stays roughly one control-row tall (brand / icon bulk).
        assert header_box["height"] <= max(brand_box["height"], bulk_box["height"]) + 8

        temp_c = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-compact',
        )
        temp_f = page.locator(
            '.ep1-header-status-metric[data-metric="temperature-f"] .ep1-header-status-compact',
        )
        humidity = page.locator(
            '.ep1-header-status-metric[data-metric="humidity"] .ep1-header-status-compact',
        )
        c_box = temp_c.bounding_box()
        f_box = temp_f.bounding_box()
        humidity_box = humidity.bounding_box()
        assert c_box is not None
        assert f_box is not None
        assert humidity_box is not None
        # Column 1: °C above °F; humidity sits beside °C (second column).
        assert c_box["y"] < f_box["y"]
        assert abs(c_box["y"] - humidity_box["y"]) < 4
        assert humidity_box["x"] > c_box["x"]
        assert "OFF" in page.locator(".tile-header-global-off").inner_text()
        assert "all" in page.locator(".tile-header-global-off").inner_text()
    finally:
        page.close()


def test_ep1_header_status_module_readings_only_contract() -> None:
    src = _EP1_HEADER_TS.read_text(encoding="utf-8")
    assert "export interface Ep1HeaderStatusSnapshot" in src
    assert "export const MOCK_EP1_HEADER_STATUS" in src
    assert "export function createEp1HeaderStatusStrip" in src
    assert "TODO(ep1-header-live)" in src
    assert "ep1-header-status-label" not in src
    assert "compactC" in src
    assert "compactF" in src
    assert 'data-metric="temperature-f"' in src or 'createMetricSpan("temperature-f"' in src
    assert "°C/" not in src
    assert "temperature_c" in src
    assert "humidity_pct" in src
    assert "illuminance_lx" in src


def test_index_html_ep1_header_status_css_contract() -> None:
    style = _extract_index_html_style_block()
    base = _css_rule_block(style, ".ep1-header-status")
    assert "display: flex" in base
    assert "flex: 0 1 auto" in base
    assert "ep1-header-status-label" not in style
    compact_only = _css_rule_block(style, ".ep1-header-status-metric-compact-only")
    assert "display: none" in compact_only
    header = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header.tile-header-global',
    )
    assert "flex-wrap: nowrap" in header
    device = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-device',
    )
    assert "grid-auto-flow: column" in device
    assert "grid-template-rows: auto auto" in device
    compact_strip = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status',
    )
    assert "max-height: 32px" in compact_strip
    compact_f = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-metric-compact-only',
    )
    assert "display: inline" in compact_f
    icons = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header-global-off',
    )
    assert "inline-flex" in icons


def test_main_uses_icon_bulk_off_on_compact() -> None:
    src = _MAIN_TS.read_text(encoding="utf-8")
    assert "createGlobalBulkOffButton()" in src
    assert 'GLOBAL_BULK_OFF_LABEL = "Turn off / pause / close everything"' in src
    assert "tile-header-global-off-icons" in src
    assert 'textContent = "OFF"' in src
    assert 'textContent = "all"' in src
    assert "createBulkOffPauseIcon" in src
    assert "createBulkOffPadlockIcon" in src
    assert "appendEp1HeaderStatusStrip(header)" in src


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
