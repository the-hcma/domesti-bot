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
def test_ep1_header_status_compact_shows_climate_dual_temp_one_row(
    chromium_browser: Any,
) -> None:
    """Phone: climate/light only, °F in compact temp, same header row as bulk-off."""
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app" data-layout="compact">
  <header class="tile-header tile-header-global">
    <span class="brand-mark">Domesti</span>
    <aside class="ep1-header-status" data-mock="true" aria-label="Room sensors">
      <div class="ep1-header-status-device">
        <span class="ep1-header-status-label">Office EP1</span>
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
      <button type="button" class="btn btn-bulk tile-header-global-off">Turn off</button>
    </div>
  </header>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(html)
        assert page.locator('[data-metric="occupancy"]').count() == 0
        temp_full = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-full',
        )
        temp_compact = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-compact',
        )
        assert temp_full.evaluate("el => getComputedStyle(el).display") == "none"
        assert temp_compact.evaluate("el => getComputedStyle(el).display") == "inline"
        assert temp_compact.inner_text() == "22.5°C/72.5°F"
        assert "°F" in temp_compact.inner_text()

        header = page.locator(".tile-header.tile-header-global")
        brand = page.locator(".brand-mark")
        strip = page.locator(".ep1-header-status")
        bulk = page.locator(".tile-header-global-off")
        header_top = header.bounding_box()
        brand_box = brand.bounding_box()
        strip_box = strip.bounding_box()
        bulk_box = bulk.bounding_box()
        assert header_top is not None
        assert brand_box is not None
        assert strip_box is not None
        assert bulk_box is not None
        # One row: brand, strip, and bulk share roughly the same vertical band.
        mid_y = brand_box["y"] + brand_box["height"] / 2
        assert abs(strip_box["y"] + strip_box["height"] / 2 - mid_y) < 20
        assert abs(bulk_box["y"] + bulk_box["height"] / 2 - mid_y) < 20
        assert strip_box["y"] < bulk_box["y"] + bulk_box["height"]
    finally:
        page.close()


def test_ep1_header_status_module_climate_only_contract() -> None:
    src = _EP1_HEADER_TS.read_text(encoding="utf-8")
    assert "export interface Ep1HeaderStatusSnapshot" in src
    assert "export const MOCK_EP1_HEADER_STATUS" in src
    assert "export function createEp1HeaderStatusStrip" in src
    assert "TODO(ep1-header-live)" in src
    assert "formatEp1HeaderOccupancy" not in src
    assert 'data-metric="occupancy"' not in src
    assert 'compact: "Occ"' not in src
    assert 'full: "Occupied"' not in src
    assert "temperature_c" in src
    assert "temperature_f" in src
    assert "humidity_pct" in src
    assert "illuminance_lx" in src
    assert "°C/" in src
    assert "${celsius.toFixed(1)} °C" in src
    assert "°F" in src
    assert "${cLabel} / ${fLabel}" in src


def test_index_html_ep1_header_status_css_contract() -> None:
    style = _extract_index_html_style_block()
    base = _css_rule_block(style, ".ep1-header-status")
    assert "display: flex" in base
    assert "flex-wrap: nowrap" in base
    assert "data-occupancy" not in style
    header = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header.tile-header-global',
    )
    assert "flex-wrap: nowrap" in header
    compact = _css_rule_block(style, '#app[data-layout="compact"] .ep1-header-status')
    assert "flex: 1 1 auto" in compact
    assert "order: 3" not in compact
    assert "flex: 1 1 100%" not in compact
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
