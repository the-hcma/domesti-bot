"""Compact- and comfortable-tile label overflow checks for ``app/api/static/index.html``.

Production CSS is inlined in the landing page. Tests here ensure very long device
names stay inside saturated tiles at phone, tablet, and desktop viewport widths.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_COMPACT_LABEL_RULE_NEEDLE = (
    '#app[data-layout="compact"] .tile-saturated-text .tile-saturated-label'
)
_COMFORTABLE_LABEL_RULE_NEEDLE = (
    '#app[data-layout="comfortable"] .tile-saturated-label'
)
_LONG_DEVICE_LABEL = (
    "Basement workshop outlet strip north wall "
    "Basement workshop outlet strip south wall "
    "Basement workshop outlet strip east wall "
    "Basement workshop outlet strip west wall"
)
_PROBE_ICON_SVG = (
    '<svg class="tile-saturated-icon" viewBox="0 0 24 24" aria-hidden="true">'
    '<path fill="none" stroke="currentColor" stroke-width="1.75" '
    'd="M9 18h6M10 22h4M15 9V7a3 3 0 0 0-6 0v2M12 2v1"/>'
    "</svg>"
)
LayoutMode = Literal["compact", "comfortable"]


def _assert_box_contains(
    inner: dict[str, float],
    outer: dict[str, float],
    *,
    tolerance_px: float = 1.0,
) -> None:
    tol = tolerance_px
    assert inner["x"] >= outer["x"] - tol
    assert inner["y"] >= outer["y"] - tol
    assert inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tol
    assert inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tol


def _assert_long_label_contained_in_tile(
    page: Any,
    *,
    layout: LayoutMode,
    min_font_px: float,
    min_label_height_px: float,
) -> None:
    if layout == "compact":
        tile = page.locator(".tile-compact")
        hit = page.locator(".tile-compact-hit")
        icon_wrap = page.locator(".tile-saturated-icon-wrap")
        text_zone = page.locator(".tile-saturated-text")
        label = page.locator(".tile-saturated-label")
    else:
        tile = page.locator(".tile-rich")
        hit = page.locator(".tile-rich-hit")
        icon_wrap = page.locator(".tile-saturated-icon-wrap")
        text_zone = hit
        label = page.locator(".tile-saturated-label")

    tile_box = tile.bounding_box()
    hit_box = hit.bounding_box()
    icon_box = icon_wrap.bounding_box()
    text_box = text_zone.bounding_box()
    label_box = label.bounding_box()
    assert tile_box is not None
    assert hit_box is not None
    assert icon_box is not None
    assert text_box is not None
    assert label_box is not None
    _assert_box_contains(hit_box, tile_box)
    _assert_box_contains(label_box, hit_box)
    _assert_box_contains(label_box, text_box)
    assert icon_box["y"] + icon_box["height"] <= label_box["y"] + 1.5
    font_size = label.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
    label_height = label.evaluate("el => el.getBoundingClientRect().height")
    assert font_size >= min_font_px
    assert label_height >= min_label_height_px
    assert label_height <= text_box["height"] + 1.5


def _build_layout_probe_html(
    *,
    label: str,
    layout: LayoutMode,
    style_css: str,
) -> str:
    safe_label = html.escape(label, quote=True)
    if layout == "compact":
        tile_markup = f"""
          <article class="tile-compact tile-switch">
            <button type="button" class="tile-compact-hit" data-tone="active">
              <span class="tile-saturated-icon-wrap">{_PROBE_ICON_SVG}</span>
              <div class="tile-saturated-text">
                <span class="tile-saturated-label">{safe_label}</span>
              </div>
            </button>
          </article>"""
        grid_class = "tile-grid tile-grid-compact"
    else:
        tile_markup = f"""
          <article class="tile-rich tile-switch">
            <button type="button" class="tile-rich-hit" data-tone="active">
              <span class="tile-saturated-icon-wrap">{_PROBE_ICON_SVG}</span>
              <span class="tile-saturated-label">{safe_label}</span>
            </button>
          </article>"""
        grid_class = "tile-grid"
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{style_css}</style>
</head>
<body>
  <main>
    <div id="app" data-layout="{layout}">
      <section class="family" data-connected="true">
        <div class="{grid_class}">
          {tile_markup}
        </div>
      </section>
    </div>
  </main>
</body>
</html>"""


def _comfortable_label_rule_block(style_css: str) -> str:
    return _css_rule_block(style_css, _COMFORTABLE_LABEL_RULE_NEEDLE)


def _compact_label_rule_block(style_css: str) -> str:
    return _css_rule_block(style_css, _COMPACT_LABEL_RULE_NEEDLE)


def _css_rule_block(style_css: str, selector_needle: str) -> str:
    search_from = 0
    idx = -1
    while search_from < len(style_css):
        candidate = style_css.find(selector_needle, search_from)
        if candidate < 0:
            break
        after = style_css[candidate + len(selector_needle) :].lstrip()
        if after.startswith("{"):
            idx = candidate
            break
        search_from = candidate + 1
    assert idx >= 0, f"Expected rule {selector_needle!r} in index.html <style>"
    brace = style_css.index("{", idx)
    depth = 0
    for offset, char in enumerate(style_css[brace:]):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = brace + offset + 1
                return style_css[idx:end]
    msg = f"Unclosed CSS rule for {selector_needle!r}"
    raise AssertionError(msg)


def _extract_index_html_style_block() -> str:
    raw = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    start = raw.index("<style>") + len("<style>")
    end = raw.index("</style>", start)
    return raw[start:end]


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


def test_index_html_tile_label_css_clips_overflow() -> None:
    """Landing-page CSS must clip long labels for compact and comfortable tiles."""

    style = _extract_index_html_style_block()
    compact_label_rule = _compact_label_rule_block(style)
    comfortable_label_rule = _comfortable_label_rule_block(style)
    hit_rule = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-compact-hit',
    )
    text_zone_rule = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-saturated-text',
    )
    for fragment in (
        "overflow: hidden",
        "max-height: 100%",
        "-webkit-line-clamp: 3",
        "line-clamp: 3",
        "overflow-wrap: anywhere",
        "font-size: clamp(",
        "cqw",
    ):
        assert fragment in compact_label_rule, fragment
    for fragment in (
        "overflow: hidden",
        "-webkit-line-clamp: 2",
        "line-clamp: 2",
        "overflow-wrap: break-word",
    ):
        assert fragment in comfortable_label_rule, fragment
    assert "overflow: hidden" in text_zone_rule
    assert "overflow: hidden" in hit_rule
    tile_shell_rule = _css_rule_block(style, ".tile-compact, .tile-rich")
    assert "overflow: hidden" in tile_shell_rule
    assert re.search(
        r"grid-template-rows:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1fr\)",
        hit_rule,
    )


@pytest.mark.browser
@pytest.mark.parametrize(
    (
        "viewport_name",
        "viewport_width",
        "viewport_height",
        "layout",
        "min_font_px",
        "min_label_height_px",
    ),
    [
        pytest.param(
            "compact-narrow-phone",
            320,
            568,
            "compact",
            11.0,
            16.0,
            id="320-compact",
        ),
        pytest.param(
            "compact-phone",
            390,
            844,
            "compact",
            11.0,
            20.0,
            id="390-compact",
        ),
        pytest.param(
            "compact-tablet",
            768,
            1024,
            "compact",
            16.0,
            32.0,
            id="768-compact",
        ),
        pytest.param(
            "comfortable-laptop",
            1024,
            768,
            "comfortable",
            14.0,
            28.0,
            id="1024-comfortable",
        ),
        pytest.param(
            "comfortable-wide",
            1440,
            900,
            "comfortable",
            14.0,
            28.0,
            id="1440-comfortable",
        ),
    ],
)
def test_long_device_label_stays_inside_tile_at_viewport(
    chromium_browser: Any,
    viewport_name: str,
    viewport_width: int,
    viewport_height: int,
    layout: LayoutMode,
    min_font_px: float,
    min_label_height_px: float,
) -> None:
    """Long labels must not spill outside tiles from narrow phones through wide web UI."""

    _ = viewport_name
    style_css = _extract_index_html_style_block()
    document = _build_layout_probe_html(
        label=_LONG_DEVICE_LABEL,
        layout=layout,
        style_css=style_css,
    )
    context = chromium_browser.new_context(
        viewport={"width": viewport_width, "height": viewport_height},
    )
    page = context.new_page()
    try:
        page.set_content(document, wait_until="load")
        _assert_long_label_contained_in_tile(
            page,
            layout=layout,
            min_font_px=min_font_px,
            min_label_height_px=min_label_height_px,
        )
    finally:
        context.close()
