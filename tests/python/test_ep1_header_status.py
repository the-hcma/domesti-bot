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
def test_ep1_header_status_compact_single_column_stack(
    chromium_browser: Any,
) -> None:
    """Phone: °C, °F, humidity, lux stacked in one column."""
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app" data-layout="compact">
  <header class="tile-header tile-header-global">
    <span class="brand-mark">
      <svg class="brand-mark-svg" width="32" height="32" viewBox="0 0 24 24"></svg>
    </span>
    <aside class="ep1-header-status" aria-label="Room sensors">
      <div class="ep1-header-status-device" title="Office EP1">
        <span class="ep1-header-status-metric" data-metric="temperature">
          <span class="ep1-header-status-full">22.5 °C</span>
          <span class="ep1-header-status-compact">22.5°C</span>
        </span>
        <span class="ep1-header-status-metric" data-metric="temperature-f">
          <span class="ep1-header-status-full">72.5 °F</span>
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
      <div class="tile-header-end-icons">
        <span class="ep1-header-occupancy-glyph" data-occupancy="clear"
              role="img" aria-label="Room clear">
          <svg class="ep1-header-occupancy-svg" width="20" height="20"
               viewBox="0 0 24 24" aria-hidden="true"></svg>
        </span>
        <button type="button" class="btn btn-theme-toggle" aria-label="Theme">
          <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true"></svg>
        </button>
      </div>
    </div>
  </header>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 390, "height": 844})
    try:
        page.set_content(html)
        c_box = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-compact',
        ).bounding_box()
        f_box = page.locator(
            '.ep1-header-status-metric[data-metric="temperature-f"] .ep1-header-status-compact',
        ).bounding_box()
        humidity_box = page.locator(
            '.ep1-header-status-metric[data-metric="humidity"] .ep1-header-status-compact',
        ).bounding_box()
        lux_box = page.locator(
            '.ep1-header-status-metric[data-metric="illuminance"] .ep1-header-status-compact',
        ).bounding_box()
        assert c_box is not None
        assert f_box is not None
        assert humidity_box is not None
        assert lux_box is not None
        # Single column: each metric below the previous, same x.
        assert c_box["y"] < f_box["y"] < humidity_box["y"] < lux_box["y"]
        assert abs(c_box["x"] - f_box["x"]) < 4
        assert abs(c_box["x"] - humidity_box["x"]) < 4
        assert abs(c_box["x"] - lux_box["x"]) < 4
        assert "OFF" in page.locator(".tile-header-global-off").inner_text()
        assert "all" in page.locator(".tile-header-global-off").inner_text()
        bulk = page.locator(".tile-header-global-off").bounding_box()
        end = page.locator(".tile-header-end-icons").bounding_box()
        glyph = page.locator(".ep1-header-occupancy-glyph").bounding_box()
        theme = page.locator(".btn-theme-toggle").bounding_box()
        assert bulk is not None and end is not None
        assert glyph is not None and theme is not None
        # Bulk-off sits left of the end-icon pair; pair stays compact.
        assert bulk["x"] + bulk["width"] <= end["x"] + 2
        assert end["width"] < bulk["width"]
        assert glyph["width"] <= 36
        assert theme["width"] <= 36
        # Compact end-icons stay short: beat ≤600px .btn { min-height: 44px }.
        assert glyph["height"] <= 36
        assert theme["height"] <= 36
        assert abs(glyph["height"] - theme["height"]) < 8
    finally:
        page.close()


@pytest.mark.browser
def test_ep1_header_status_comfortable_splits_c_and_f_with_dot(
    chromium_browser: Any,
) -> None:
    """Desktop: °C and °F are separate metrics with the same · separator."""
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app" data-layout="comfortable">
  <aside class="ep1-header-status" aria-label="Room sensors">
    <div class="ep1-header-status-device">
      <span class="ep1-header-status-metric" data-metric="temperature">
        <span class="ep1-header-status-full">22.5 °C</span>
        <span class="ep1-header-status-compact">22.5°C</span>
      </span>
      <span class="ep1-header-status-metric" data-metric="temperature-f">
        <span class="ep1-header-status-full">72.5 °F</span>
        <span class="ep1-header-status-compact">72.5°F</span>
      </span>
      <span class="ep1-header-status-metric" data-metric="humidity">
        <span class="ep1-header-status-full">42%</span>
        <span class="ep1-header-status-compact">42%</span>
      </span>
    </div>
  </aside>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.set_content(html)
        c_box = page.locator(
            '.ep1-header-status-metric[data-metric="temperature"] .ep1-header-status-full',
        ).bounding_box()
        f_box = page.locator(
            '.ep1-header-status-metric[data-metric="temperature-f"] .ep1-header-status-full',
        ).bounding_box()
        humidity_box = page.locator(
            '.ep1-header-status-metric[data-metric="humidity"] .ep1-header-status-full',
        ).bounding_box()
        assert c_box is not None
        assert f_box is not None
        assert humidity_box is not None
        assert abs(c_box["y"] - f_box["y"]) < 4
        assert f_box["x"] > c_box["x"]
        assert humidity_box["x"] > f_box["x"]
        before = page.evaluate(
            """() => {
              const f = document.querySelector(
                '.ep1-header-status-metric[data-metric="temperature-f"]'
              );
              return f ? getComputedStyle(f, '::before').content : null;
            }""",
        )
        assert before is not None
        assert "·" in before or '"·"' in before or before == '"·"'
    finally:
        page.close()


@pytest.mark.browser
def test_ep1_header_occupancy_glyph_contrasts_on_canvas_in_light_and_dark(
    chromium_browser: Any,
) -> None:
    """Clear ghost stays visible on the dark canvas in both appearances."""
    style_css = _extract_index_html_style_block()
    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><style>{style_css}</style></head>
<body>
<div id="app">
  <span class="ep1-header-occupancy-glyph" data-occupancy="clear"></span>
  <span class="ep1-header-occupancy-glyph" data-occupancy="occupied"></span>
</div>
</body></html>"""
    page = chromium_browser.new_page(viewport={"width": 1280, "height": 800})
    try:
        page.set_content(html)
        for theme in ("light", "dark"):
            page.evaluate(
                "(theme) => document.documentElement.setAttribute('data-theme', theme)",
                theme,
            )
            colors = page.evaluate(
                """() => {
                  const canvas = getComputedStyle(document.documentElement)
                    .getPropertyValue('--canvas-bg').trim();
                  const clear = getComputedStyle(
                    document.querySelector('[data-occupancy="clear"]')
                  ).color;
                  const occupied = getComputedStyle(
                    document.querySelector('[data-occupancy="occupied"]')
                  ).color;
                  return { canvas, clear, occupied };
                }""",
            )
            canvas = _parse_css_color(str(colors["canvas"]))
            clear = _parse_css_color(str(colors["clear"]))
            occupied = _parse_css_color(str(colors["occupied"]))
            assert _contrast_ratio(clear, canvas) >= 3.0, (
                f"clear glyph vs canvas in {theme}: {colors['clear']} on {colors['canvas']}"
            )
            assert _contrast_ratio(occupied, canvas) >= 3.0, (
                f"occupied glyph vs canvas in {theme}: {colors['occupied']} on {colors['canvas']}"
            )
    finally:
        page.close()


def test_ep1_header_status_module_readings_only_contract() -> None:
    src = _EP1_HEADER_TS.read_text(encoding="utf-8")
    assert "export interface Ep1HeaderStatusSnapshot" in src
    assert "responding: boolean" in src
    assert "export const MOCK_EP1_HEADER_STATUS" not in src
    assert "TODO(ep1-header-live)" not in src
    assert "export function createEp1HeaderStatusStrip" in src
    assert "export function ep1HeaderStatusFromUiState" in src
    assert "export function ep1HeaderOccupancyGlyphFromUiState" in src
    assert "export function createEp1HeaderOccupancyGlyph" in src
    assert "export function formatEp1HeaderOccupancyTooltip" in src
    assert "formatDeviceIdentityTooltip" in src
    assert "mac_address" in src
    assert "ep1-header-status-label" not in src
    assert "compactC" in src
    assert "compactF" in src
    assert "fullC" in src
    assert "fullF" in src
    assert 'createMetricSpan(\n        "temperature-f"' in src or '"temperature-f"' in src
    assert "ep1-header-status-metric-compact-only" not in src
    assert "°C /" not in src
    assert "temperature_c" in src
    assert "humidity_pct" in src
    assert "illuminance_lx" in src
    assert 'dataset["responding"]' in src
    assert "EP1_HEADER_STALE_AFTER_S" in src


def test_index_html_ep1_header_status_css_contract() -> None:
    style = _extract_index_html_style_block()
    base = _css_rule_block(style, ".ep1-header-status")
    assert "display: flex" in base
    assert "flex: 0 1 auto" in base
    assert "ep1-header-status-label" not in style
    assert "ep1-header-status-metric-compact-only" not in style
    responding = _css_rule_block(
        style,
        '.ep1-header-status-metric[data-responding="true"]',
    )
    stale = _css_rule_block(
        style,
        '.ep1-header-status-metric[data-responding="false"]',
    )
    assert "var(--accent)" in responding
    assert "var(--pending)" in stale
    glyph = _css_rule_block(style, ".ep1-header-occupancy-glyph")
    assert "inline-flex" in glyph
    assert "min-width: 44px" not in glyph
    occupied = _css_rule_block(
        style,
        '.ep1-header-occupancy-glyph[data-occupancy="occupied"]',
    )
    clear = _css_rule_block(
        style,
        '.ep1-header-occupancy-glyph[data-occupancy="clear"]',
    )
    fill = _css_rule_block(style, ".ep1-header-occupancy-fill")
    assert "var(--accent)" in occupied
    assert "#cfd3db" in clear
    assert "color-mix" in fill
    end_icons = _css_rule_block(style, ".tile-header-end-icons")
    assert "grid-template-columns: auto auto" in end_icons
    header = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header.tile-header-global',
    )
    assert "flex-wrap: nowrap" in header
    device = _css_rule_block(
        style,
        '#app[data-layout="compact"] .ep1-header-status-device',
    )
    assert "flex-direction: column" in device
    assert "grid-auto-flow: column" not in device
    icons = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header-global-off',
    )
    assert "inline-flex" in icons
    compact_theme = _css_rule_block(
        style,
        '#app[data-layout="compact"] .btn.btn-theme-toggle',
    )
    assert "min-height: 0" in compact_theme
    theme_toggle = _css_rule_block(style, ".btn.btn-theme-toggle")
    assert "padding: 4px" in theme_toggle
    assert "min-height: 0" in theme_toggle
    metric_sep = _css_rule_block(style, ".ep1-header-status-metric ~ .ep1-header-status-metric::before")
    assert "·" in metric_sep


def test_main_uses_icon_bulk_off_on_compact() -> None:
    src = _MAIN_TS.read_text(encoding="utf-8")
    assert "createGlobalBulkOffButton()" in src
    assert 'GLOBAL_BULK_OFF_LABEL = "Turn off / pause / close everything"' in src
    assert "tile-header-global-off-icons" in src
    assert 'textContent = "OFF"' in src
    assert 'textContent = "all"' in src
    assert "createBulkOffPauseIcon" in src
    assert "createBulkOffPadlockIcon" in src
    assert "appendEp1HeaderStatusStrip(header, state)" in src
    assert "ep1HeaderStatusFromUiState(state)" in src
    assert "createTileHeaderEndIcons(state)" in src
    assert "createTileHeaderEndIcons(null)" in src
    assert "ep1HeaderOccupancyGlyphFromUiState(state)" in src
    assert "createEp1HeaderOccupancyGlyph(" in src
    assert "ep1HeaderStatusFromUiState(state)" in src
    assert "formatDeviceIdentityTooltip" in src
    assert "function deviceIdentityTooltip" not in src
    assert 'className = "tile-header-end-icons"' in src
    assert "MOCK_EP1_HEADER_STATUS" not in src
    assert "TODO(ep1-header-live)" not in src
    # Sparse error/loading headers use the same end-icons wrapper as main/empty.
    assert src.count("createTileHeaderEndIcons(null)") >= 3
    assert "errHead.append(createBrandMark(this.meta), createTileHeaderEndIcons(null))" in src
    assert "loadHead.append(createBrandMark(this.meta), createTileHeaderEndIcons(null))" in src
    # Landscape phones: height + coarse pointer, not width alone (see COMPACT_LAYOUT_MQ).
    assert "max-height: 560px" in src
    assert "pointer: coarse" in src
    assert "°C · " in src


def test_device_identity_tooltip_module_contract() -> None:
    src = (_REPO_ROOT / "web" / "src" / "device-identity-tooltip.ts").read_text(
        encoding="utf-8",
    )
    assert "export const DEVICE_PROPERTIES_MENU_HINT" in src
    assert "Right-click to edit device properties" in src
    assert "export function formatDeviceIdentityTooltip" in src
    assert "MAC address:" in src
    assert "IP:" in src
    assert "includeLabel" in src
    assert "includePropertiesHint" in src


def test_compact_layout_mq_css_matches_main() -> None:
    """Phone compact MQ in main.ts and index.html @media must be identical."""
    main_mq = _compact_layout_mq_from_main(_MAIN_TS.read_text(encoding="utf-8"))
    css_mq = _compact_layout_mq_from_index_style(_extract_index_html_style_block())
    assert main_mq == css_mq
    assert "max-height: 560px" in main_mq
    assert "hover: none" in main_mq
    assert "pointer: coarse" in main_mq
    assert "max-width: 768px" in main_mq


def _compact_layout_mq_from_index_style(style: str) -> str:
    match = re.search(
        r"@media\s+((?:[^{]|\n)*max-height:\s*560px(?:[^{]|\n)*)\{",
        style,
        flags=re.DOTALL,
    )
    assert match is not None, "Expected landscape-aware @media in index.html <style>"
    return _normalize_media_query_clause(match.group(1))


def _compact_layout_mq_from_main(src: str) -> str:
    match = re.search(
        r'const COMPACT_LAYOUT_MQ\s*=\s*"([^"]+)"',
        src,
        flags=re.DOTALL,
    )
    assert match is not None, "Expected COMPACT_LAYOUT_MQ string in main.ts"
    return _normalize_media_query_clause(match.group(1))


def _contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    lighter = max(_relative_luminance(fg), _relative_luminance(bg))
    darker = min(_relative_luminance(fg), _relative_luminance(bg))
    return (lighter + 0.05) / (darker + 0.05)


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


def _normalize_media_query_clause(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())


def _parse_css_color(raw: str) -> tuple[int, int, int]:
    text = raw.strip()
    hex_match = re.fullmatch(r"#([0-9a-fA-F]{6})", text)
    if hex_match is not None:
        digits = hex_match.group(1)
        return (int(digits[0:2], 16), int(digits[2:4], 16), int(digits[4:6], 16))
    rgb_match = re.fullmatch(
        r"rgba?\(\s*(\d+)(?:\s*,\s*|\s+)(\d+)(?:\s*,\s*|\s+)(\d+)(?:\s*[,/]\s*[\d.]+)?\s*\)",
        text,
    )
    if rgb_match is not None:
        return (int(rgb_match.group(1)), int(rgb_match.group(2)), int(rgb_match.group(3)))
    raise AssertionError(f"Expected hex or rgb() color, got {raw!r}")


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        scaled = value / 255.0
        if scaled <= 0.03928:
            return scaled / 12.92
        return ((scaled + 0.055) / 1.055) ** 2.4

    red, green, blue = rgb
    return 0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue)
