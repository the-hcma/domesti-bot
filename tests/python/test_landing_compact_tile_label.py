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

from app.ui_compact_icon import resolve_compact_icon

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COMPACT_ICONS_DIR = _REPO_ROOT / "app" / "api" / "static" / "icons" / "compact"
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_COMPACT_LABEL_RULE_NEEDLE = '#app[data-layout="compact"] .tile-saturated-text .tile-saturated-label'
_COMFORTABLE_LABEL_RULE_NEEDLE = '#app[data-layout="comfortable"] .tile-saturated-label'
_LONG_DEVICE_LABEL = (
    "Basement workshop outlet strip north wall "
    "Basement workshop outlet strip south wall "
    "Basement workshop outlet strip east wall "
    "Basement workshop outlet strip west wall"
)
_GLOBAL_BULK_LABEL = "Turn off / pause / close everything"
# Mirrors createGlobalBulkOffButton compact branch in web/src/main.ts.
_GLOBAL_BULK_ICON_MARKUP = (
    '<span class="tile-header-global-off-glyph tile-header-global-off-off"'
    ' aria-hidden="true">OFF</span>'
    '<svg class="tile-header-global-off-glyph tile-header-global-off-svg"'
    ' viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
    '<rect x="6" y="5" width="4" height="14" rx="1"/>'
    '<rect x="14" y="5" width="4" height="14" rx="1"/></svg>'
    '<svg class="tile-header-global-off-glyph tile-header-global-off-svg"'
    ' viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    ' stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"'
    ' aria-hidden="true">'
    '<path d="M7 11V8a5 5 0 0 1 10 0v3"/>'
    '<rect x="5" y="11" width="14" height="10" rx="2"/></svg>'
    '<span class="tile-header-global-off-glyph tile-header-global-off-all"'
    ' aria-hidden="true">all</span>'
)
# Mobile-mock Kasa names (short room labels + one object-style alias).
_MOCK_KASA_GRID_TILES: tuple[tuple[str, str], ...] = (
    ("Kitchen", "active"),
    ("Porch", "inactive"),
    ("Office", "inactive"),
    ("Hall", "active"),
    ("Guest", "inactive"),
    ("Basement", "inactive"),
    ("Basement lamp", "inactive"),
)
# Keep in sync with web/src/main.ts (COMPACT_*_FONT_*_PX).
_COMPACT_LABEL_FONT_MIN_PX = 11
_COMPACT_LABEL_FONT_MAX_PX = 30
_COMPACT_BULK_FONT_MIN_PX = 11
_COMPACT_BULK_FONT_MAX_PX = 18
# Mirrors largestCompactLabelFontPx in main.ts (tile labels only).
_COMPACT_LABEL_BINARY_SEARCH_JS = """
(appRoot, bounds) => {
  const [minPx, maxPx] = bounds;
  const labels = [...appRoot.querySelectorAll(".tile-compact .tile-saturated-label")];
  if (labels.length === 0) {
    return null;
  }
  const labelFits = (px) => {
    appRoot.style.setProperty("--compact-tile-label-px", `${px}px`);
    return labels.every((label) =>
      label.scrollHeight <= label.clientHeight + 1
      && label.scrollWidth <= label.clientWidth + 1
    );
  };
  let lo = minPx;
  let hi = maxPx;
  let best = lo;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (labelFits(mid)) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  appRoot.style.setProperty("--compact-tile-label-px", `${best}px`);
  return best;
}
"""
# Mirrors syncCompactTypographyFit in main.ts.
_SYNC_COMPACT_TYPOGRAPHY_FIT_JS = """
(appRoot, bounds) => {
  const [labelBounds, bulkBounds] = bounds;
  const [labelMin, labelMax] = labelBounds;
  const [bulkMin, bulkMax] = bulkBounds;
  const labels = [...appRoot.querySelectorAll(".tile-compact .tile-saturated-label")];
  const bulkBtn = appRoot.querySelector(".tile-header-global-off");
  let labelPx = null;
  if (labels.length > 0) {
    const labelFits = (px) => {
      appRoot.style.setProperty("--compact-tile-label-px", `${px}px`);
      return labels.every((label) =>
        label.scrollHeight <= label.clientHeight + 1
        && label.scrollWidth <= label.clientWidth + 1
      );
    };
    let lo = labelMin;
    let hi = labelMax;
    let best = lo;
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (labelFits(mid)) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    labelPx = best;
    appRoot.style.setProperty("--compact-tile-label-px", `${best}px`);
  }
  if (bulkBtn !== null) {
    const bulkFits = (px) => {
      appRoot.style.setProperty("--compact-global-bulk-px", `${px}px`);
      return (
        bulkBtn.scrollHeight <= bulkBtn.clientHeight + 1
        && bulkBtn.scrollWidth <= bulkBtn.clientWidth + 1
      );
    };
    let lo = bulkMin;
    let hi = bulkMax;
    let best = lo;
    while (lo <= hi) {
      const mid = Math.floor((lo + hi) / 2);
      if (bulkFits(mid)) {
        best = mid;
        lo = mid + 1;
      } else {
        hi = mid - 1;
      }
    }
    appRoot.style.setProperty("--compact-global-bulk-px", `${best}px`);
  }
  return labelPx;
}
"""
_PROBE_ICON_MARKUP = (
    '<span class="tile-saturated-icon-host">'
    '<svg class="tile-saturated-icon" viewBox="0 0 24 24" fill="none" '
    'stroke="currentColor" stroke-width="2" aria-hidden="true">'
    '<path d="M9 18h6M10 22h4M12 2v1"/></svg></span>'
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


def _assert_all_compact_labels_contained_in_tiles(page: Any) -> None:
    tile_count = page.locator(".tile-compact").count()
    assert tile_count > 0
    for index in range(tile_count):
        tile = page.locator(".tile-compact").nth(index)
        hit = tile.locator(".tile-compact-hit")
        text_zone = tile.locator(".tile-saturated-text")
        label = tile.locator(".tile-saturated-label")
        tile_box = tile.bounding_box()
        hit_box = hit.bounding_box()
        text_box = text_zone.bounding_box()
        label_box = label.bounding_box()
        assert tile_box is not None
        assert hit_box is not None
        assert text_box is not None
        assert label_box is not None
        _assert_box_contains(hit_box, tile_box)
        _assert_box_contains(label_box, hit_box)
        _assert_box_contains(label_box, text_box)
        assert label_box["height"] <= text_box["height"] + 1.5


def _assert_compact_global_bulk_button_fits(page: Any) -> None:
    bulk = page.locator(".tile-header-global-off")
    fits = bulk.evaluate(
        """el => (
          el.scrollHeight <= el.clientHeight + 1
          && el.scrollWidth <= el.clientWidth + 1
        )"""
    )
    assert fits is True


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


def _build_compact_typography_probe_html(
    *,
    device_label: str,
    style_css: str,
    include_global_bulk: bool,
    extra_style: str = "",
) -> str:
    safe_label = html.escape(device_label, quote=True)
    bulk_markup = ""
    if include_global_bulk:
        safe_aria = html.escape(_GLOBAL_BULK_LABEL, quote=True)
        bulk_markup = f"""
      <header class="tile-header tile-header-global">
        <div class="tile-header-actions">
          <button type="button"
                  class="btn btn-bulk tile-header-global-off tile-header-global-off-icons"
                  aria-label="{safe_aria}">{_GLOBAL_BULK_ICON_MARKUP}</button>
        </div>
      </header>"""
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{style_css}{extra_style}</style>
</head>
<body>
  <main>
    <div id="app" data-layout="compact">
      {bulk_markup}
      <section class="family" data-connected="true">
        <div class="tile-grid tile-grid-compact">
          <article class="tile-compact tile-switch">
            <button type="button" class="tile-compact-hit" data-tone="active">
              <span class="tile-saturated-icon-wrap">{_PROBE_ICON_MARKUP}</span>
              <div class="tile-saturated-text">
                <span class="tile-saturated-label">{safe_label}</span>
              </div>
            </button>
          </article>
        </div>
      </section>
    </div>
  </main>
</body>
</html>"""


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
              <span class="tile-saturated-icon-wrap">{_PROBE_ICON_MARKUP}</span>
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
              <span class="tile-saturated-icon-wrap">{_PROBE_ICON_MARKUP}</span>
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


def _build_mock_kasa_compact_grid_probe_html(style_css: str) -> str:
    """Three-column Kasa grid with mock room names and per-label compact icons."""
    tiles_markup = "\n".join(_mock_kasa_tile_markup(label=label, tone=tone) for label, tone in _MOCK_KASA_GRID_TILES)
    safe_aria = html.escape(_GLOBAL_BULK_LABEL, quote=True)
    return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>{style_css}</style>
</head>
<body>
  <main>
    <div id="app" data-layout="compact">
      <header class="tile-header tile-header-global">
        <div class="tile-header-actions">
          <button type="button"
                  class="btn btn-bulk tile-header-global-off tile-header-global-off-icons"
                  aria-label="{safe_aria}">{_GLOBAL_BULK_ICON_MARKUP}</button>
        </div>
      </header>
      <section class="family" data-connected="true">
        <div class="tile-grid tile-grid-compact">
{tiles_markup}
        </div>
      </section>
    </div>
  </main>
</body>
</html>"""


def _compact_icon_host_markup(icon_key: str) -> str:
    raw = (_COMPACT_ICONS_DIR / f"{icon_key}.svg").read_text(encoding="utf-8")
    snippet = re.sub(r"<\?xml[^>]*\?>\s*", "", raw, count=1).strip()
    if 'class="tile-saturated-icon"' not in snippet:
        snippet = re.sub(
            r"<svg\b",
            '<svg class="tile-saturated-icon" aria-hidden="true"',
            snippet,
            count=1,
        )
    return f'<span class="tile-saturated-icon-host">{snippet}</span>'


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


def _mock_kasa_tile_markup(
    *,
    label: str,
    tone: str,
) -> str:
    icon_key = resolve_compact_icon(
        family_id="kasa",
        label=label,
        kind="switch",
    )
    icon_markup = _compact_icon_host_markup(icon_key)
    safe_label = html.escape(label, quote=True)
    return f"""          <article class="tile-compact tile-switch">
            <button type="button" class="tile-compact-hit" data-tone="{tone}">
              <span class="tile-saturated-icon-wrap">{icon_markup}</span>
              <div class="tile-saturated-text">
                <span class="tile-saturated-label">{safe_label}</span>
              </div>
            </button>
          </article>"""


def _compact_icon_asset_keys() -> set[str]:
    return {path.stem for path in _COMPACT_ICONS_DIR.glob("*.svg")}


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
    compact_bulk_rule = _css_rule_block(
        style,
        '#app[data-layout="compact"] .tile-header-global-off',
    )
    for fragment in (
        "overflow: hidden",
        "max-height: 100%",
        "-webkit-line-clamp: 3",
        "line-clamp: 3",
        "overflow-wrap: anywhere",
        "var(--compact-tile-label-px, clamp(",
        "cqw",
    ):
        assert fragment in compact_label_rule, fragment
    assert "var(--compact-global-bulk-px, clamp(" in compact_bulk_rule
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


@pytest.mark.browser
def test_compact_label_binary_search_sets_fitting_css_var(
    chromium_browser: Any,
) -> None:
    """Binary-search label sizing (main.ts) must set --compact-tile-label-px so text fits."""

    style_css = _extract_index_html_style_block()
    document = _build_compact_typography_probe_html(
        device_label=_LONG_DEVICE_LABEL,
        style_css=style_css,
        include_global_bulk=False,
    )
    context = chromium_browser.new_context(
        viewport={"width": 320, "height": 568},
    )
    page = context.new_page()
    try:
        page.set_content(document, wait_until="load")
        app_root = page.locator("#app")
        bounds = [_COMPACT_LABEL_FONT_MIN_PX, _COMPACT_LABEL_FONT_MAX_PX]
        font_px = app_root.evaluate(_COMPACT_LABEL_BINARY_SEARCH_JS, bounds)
        assert font_px is not None
        assert _COMPACT_LABEL_FONT_MIN_PX <= font_px <= _COMPACT_LABEL_FONT_MAX_PX
        assert app_root.evaluate("el => el.style.getPropertyValue('--compact-tile-label-px')") == f"{font_px}px"
        _assert_long_label_contained_in_tile(
            page,
            layout="compact",
            min_font_px=float(_COMPACT_LABEL_FONT_MIN_PX),
            min_label_height_px=16.0,
        )
    finally:
        context.close()


@pytest.mark.browser
def test_compact_global_bulk_uses_dedicated_fitted_font_size(
    chromium_browser: Any,
) -> None:
    """Global bulk-off button uses --compact-global-bulk-px capped below tile labels."""

    style_css = _extract_index_html_style_block()
    document = _build_compact_typography_probe_html(
        device_label="Kitchen",
        style_css=style_css,
        include_global_bulk=True,
    )
    context = chromium_browser.new_context(
        viewport={"width": 390, "height": 844},
    )
    page = context.new_page()
    try:
        page.set_content(document, wait_until="load")
        app_root = page.locator("#app")
        label_bounds = [_COMPACT_LABEL_FONT_MIN_PX, _COMPACT_LABEL_FONT_MAX_PX]
        bulk_bounds = [_COMPACT_BULK_FONT_MIN_PX, _COMPACT_BULK_FONT_MAX_PX]
        app_root.evaluate(
            _SYNC_COMPACT_TYPOGRAPHY_FIT_JS,
            [label_bounds, bulk_bounds],
        )
        bulk = page.locator(".tile-header-global-off")
        bulk_font = bulk.evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
        assert bulk_font <= _COMPACT_BULK_FONT_MAX_PX + 0.5
        bulk_px = app_root.evaluate("el => el.style.getPropertyValue('--compact-global-bulk-px')")
        assert bulk_px.endswith("px")
        assert float(bulk_px.removesuffix("px")) <= _COMPACT_BULK_FONT_MAX_PX + 0.5
        _assert_compact_global_bulk_button_fits(page)
    finally:
        context.close()


@pytest.mark.browser
def test_compact_label_binary_search_shrinks_when_text_zone_is_short(
    chromium_browser: Any,
) -> None:
    """A cramped text zone must drive the search below the maximum label size."""

    style_css = _extract_index_html_style_block()
    document = _build_compact_typography_probe_html(
        device_label=_LONG_DEVICE_LABEL,
        style_css=style_css,
        include_global_bulk=False,
        extra_style=('#app[data-layout="compact"] .tile-saturated-text { max-height: 2.5rem; }'),
    )
    context = chromium_browser.new_context(
        viewport={"width": 320, "height": 568},
    )
    page = context.new_page()
    try:
        page.set_content(document, wait_until="load")
        app_root = page.locator("#app")
        bounds = [_COMPACT_LABEL_FONT_MIN_PX, _COMPACT_LABEL_FONT_MAX_PX]
        font_px = app_root.evaluate(_COMPACT_LABEL_BINARY_SEARCH_JS, bounds)
        assert font_px is not None
        assert font_px < _COMPACT_LABEL_FONT_MAX_PX
        _assert_long_label_contained_in_tile(
            page,
            layout="compact",
            min_font_px=float(_COMPACT_LABEL_FONT_MIN_PX),
            min_label_height_px=16.0,
        )
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize(
    ("viewport_width", "viewport_height"),
    [
        pytest.param(320, 568, id="320x568"),
        pytest.param(390, 844, id="390x844"),
    ],
)
def test_mock_kasa_grid_compact_typography_fit_avoids_overflow(
    chromium_browser: Any,
    viewport_width: int,
    viewport_height: int,
) -> None:
    """Mock Kasa room labels + icons must fit inside compact tiles after typography sync."""

    style_css = _extract_index_html_style_block()
    document = _build_mock_kasa_compact_grid_probe_html(style_css)
    context = chromium_browser.new_context(
        viewport={"width": viewport_width, "height": viewport_height},
    )
    page = context.new_page()
    try:
        page.set_content(document, wait_until="load")
        app_root = page.locator("#app")
        label_bounds = [_COMPACT_LABEL_FONT_MIN_PX, _COMPACT_LABEL_FONT_MAX_PX]
        bulk_bounds = [_COMPACT_BULK_FONT_MIN_PX, _COMPACT_BULK_FONT_MAX_PX]
        font_px = app_root.evaluate(
            _SYNC_COMPACT_TYPOGRAPHY_FIT_JS,
            [label_bounds, bulk_bounds],
        )
        assert font_px is not None
        assert _COMPACT_LABEL_FONT_MIN_PX <= font_px <= _COMPACT_LABEL_FONT_MAX_PX
        bulk_font = page.locator(".tile-header-global-off").evaluate("el => parseFloat(getComputedStyle(el).fontSize)")
        assert bulk_font <= _COMPACT_BULK_FONT_MAX_PX + 0.5
        _assert_all_compact_labels_contained_in_tiles(page)
        _assert_compact_global_bulk_button_fits(page)
    finally:
        context.close()
