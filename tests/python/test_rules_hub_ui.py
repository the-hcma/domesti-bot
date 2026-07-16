"""Rules hub UI — CSS contract and desktop browser smoke (live API + seeded presence)."""

from __future__ import annotations

import argparse
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import uvicorn

from app.api.app import create_app
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_BUNDLE = _REPO_ROOT / "automation-rules.json.example"
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_MAIN_JS_PATH = _REPO_ROOT / "app" / "api" / "static" / "dist" / "main.js"

_HENRIQUE_AT_HOME_LAT = 41.194085
_HENRIQUE_AT_HOME_LON = -73.888365
_KRISTEN_OUTSIDE_LAT = 44.417597
_KRISTEN_OUTSIDE_LON = -72.023842

_BROWSER_BOOT_TIMEOUT_MS = 15_000
_BROWSER_GOTO_TIMEOUT_MS = 30_000
_BROWSER_INTERACTION_TIMEOUT_MS = 5_000


def _seed_rules_hub_browser_db(cache_path: Path) -> None:
    """Presence rows aligned with ``web/src/rules-mock-fixtures.ts`` coordinates."""
    replace_users(
        cache_path,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Henrique's iPhone",
                enabled=True,
            ),
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
                tracking_device_label="Kristen's iPhone",
                enabled=True,
            ),
        ],
    )
    replace_geofences(
        cache_path,
        [
            GeofenceRecord(
                geofence_id="house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.8883254,
                radius_m=250,
                enabled=True,
                owntracks_rid=None,
            ),
        ],
    )
    now = time.time()
    upsert_user_location(
        cache_path,
        UserLocationRecord(
            user_id="henrique",
            lat=_HENRIQUE_AT_HOME_LAT,
            lon=_HENRIQUE_AT_HOME_LON,
            accuracy_m=12,
            fix_at=now - 60,
            reported_at=now - 60,
            source="my-tracks",
        ),
        retention=default_location_history_retention(),
    )
    upsert_user_location(
        cache_path,
        UserLocationRecord(
            user_id="kristen",
            lat=_KRISTEN_OUTSIDE_LAT,
            lon=_KRISTEN_OUTSIDE_LON,
            accuracy_m=18,
            fix_at=now - 300,
            reported_at=now - 300,
            source="my-tracks",
        ),
        retention=default_location_history_retention(),
    )


def _switch_to_users_tab(page: Any) -> None:
    """Open Users tab and wait until its presence map has mounted."""

    page.locator('.rules-tab[data-tab="users"]').click()
    page.wait_for_function(
        """() => {
          if (document.querySelector('.rules-tab[data-tab="users"].rules-tab-active') === null) {
            return false;
          }
          return document.querySelectorAll('.rules-dialog-body .rules-presence-map-shell').length === 1;
        }""",
        timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
    )


def _users_presence_map(page: Any) -> Any:
    return page.locator(".rules-dialog-body .rules-presence-map-shell").first.locator(
        ".rules-presence-map",
    )


def _users_presence_map_shell(page: Any) -> Any:
    return page.locator(".rules-dialog-body .rules-presence-map-shell").first


def test_index_html_includes_rules_hub_css() -> None:
    """Landing-page CSS defines Rules hub chrome (tabs, source pill, geofence map)."""

    html = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    for needle in (
        "rules-source-pill",
        "rules-tab-bar",
        "rules-geofence-map",
        "rules-geofence-draw-mode",
        "rules-geofence-toolbar",
        "rules-geofence-toolbar-draw-active",
        "rules-dynamic-badge",
        "rules-condition-list",
        "rules-condition-tree",
        "rules-inspector-meta",
        "rules-device-action-group",
        "rules-enable-toggle",
        "rules-info-badge",
        "rules-inline-link",
        "rules-rule-card-top",
        "rules-rule-summary-list",
        "rules-geofence-row-focused",
        "rules-info-popover[hidden]",
        "rules-mail-test-row",
        "rules-presence-map",
        "rules-presence-map-shell",
        "rules-presence-map-legend",
        "rules-presence-map-filter-swatch",
        "rules-presence-map-filters",
        "rules-day-shortcuts",
        "automations-dialog .leaflet-bar a",
        "color-scheme: dark",
        "leaflet-tooltip.rules-presence-map-tooltip",
        "rules-presence-map-tooltip-line",
        "width: max-content",
        "leaflet@1.9.4",
    ):
        assert needle in html, needle


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def rules_hub_browser_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    db = tmp_path_factory.mktemp("rules-hub-browser") / "discovery.sqlite"
    _seed_rules_hub_browser_db(db)
    return db


@pytest.fixture(scope="module")
def landing_base_url(rules_hub_browser_cache: Path) -> Iterator[str]:
    """Serve the FastAPI app on an OS-allocated loopback port (bundle must exist)."""

    if not _MAIN_JS_PATH.is_file():
        pytest.skip("app/api/static/dist/main.js missing — run pnpm run build in web/")

    args = argparse.Namespace(
        discovery_cache=str(rules_hub_browser_cache),
        tailwind_token=None,
    )

    async def bootstrap_device_state(
        *_bootstrap_args: Any,
        **_bootstrap_kwargs: Any,
    ) -> DeviceManagersState:
        kasa_mgr = MagicMock(spec=KasaDeviceManager)
        kasa_mgr.switches = ()
        return DeviceManagersState(
            kasa_mgr=kasa_mgr,
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=rules_hub_browser_cache,
            args=argparse.Namespace(),
        )

    with (
        patch(
            "app.api.app.bootstrap_device_managers",
            bootstrap_device_state,
        ),
        patch.dict(
            os.environ,
            {"DOMESTI_AUTOMATION_RULES_FILE": str(_EXAMPLE_BUNDLE)},
            clear=False,
        ),
    ):
        app = create_app(args)
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not server.started:
            if time.monotonic() > deadline:
                pytest.fail("uvicorn did not start within 10s")
            time.sleep(0.02)
        sockets = server.servers[0].sockets
        assert sockets is not None and len(sockets) > 0
        port = sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.mark.browser
def test_rules_menu_hidden_on_compact_viewport(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """☰ menu (and Automations entry) is desktop-only — absent at phone widths."""

    context = chromium_browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.wait_for_selector("#app", timeout=_BROWSER_BOOT_TIMEOUT_MS)
        assert page.locator(".app-menu").count() == 0
    finally:
        context.close()


@pytest.mark.browser
def test_status_rule_card_title_cases_user_display_names(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Status rule presence lines use title-cased user display names, not raw ids."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        dialog.locator(".rules-rule-presence-summary").first.wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        text = dialog.inner_text()
        assert "Henrique:" in text
        assert "henrique:" not in text
        assert "Kristen:" in text
    finally:
        context.close()


@pytest.mark.browser
def test_status_rule_click_opens_rules_tab_inspector(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Status tab rule cards drill into Rules tab detail with live condition rows."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator("dialog.rules-dialog").wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        status_rule = page.locator(
            ".rules-status-rule-card .rules-card-title-btn",
        ).first
        status_rule.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        status_rule.click()
        page.locator('.rules-tab[data-tab="rules"].rules-tab-active').wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        inspector = page.locator(".rules-inspector-panel")
        inspector.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        assert inspector.locator(".rules-condition-list").count() >= 1
        assert page.locator(".rules-status-rule-card .rules-condition-list").count() == 0
    finally:
        context.close()


@pytest.mark.browser
def test_rules_hub_opens_with_file_backed_rules(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Desktop ☰ → Automations shows the example bundle and the source pill."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.wait_for_selector(".app-menu", timeout=_BROWSER_BOOT_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        source_pill = page.locator(".rules-source-pill")
        source_pill.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        assert "automation-rules.json" in source_pill.inner_text().lower()
        assert "Automations" in dialog.inner_text()
        assert "Evening arrival" in dialog.inner_text()
        page.locator('.rules-tab[data-tab="rules"]').click()
        page.get_by_text("Rules are loaded from automation-rules.json").wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        assert page.get_by_role("button", name="Add rule").count() == 0
        rules_card = page.locator(".rules-card").first
        rules_card.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        card_text = rules_card.inner_text()
        assert "Front door lights" in card_text
        assert "192.168.1.42" not in card_text
    finally:
        context.close()


@pytest.mark.browser
def test_user_presence_map_renders_osm_tiles_with_filters(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Status and Users tabs share one filtered presence map with zoom controls."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        page.locator(".rules-presence-map-filters").first.wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        page.wait_for_function(
            """() => {
              const map = document.querySelector('.rules-presence-map.leaflet-container');
              return map !== null && map.querySelectorAll('img.leaflet-tile').length > 0;
            }""",
            timeout=_BROWSER_BOOT_TIMEOUT_MS,
        )
        assert page.locator(".rules-presence-map-filter").count() >= 2
        assert page.locator(".leaflet-control-zoom").count() >= 1

        _switch_to_users_tab(page)
        page.locator(".rules-presence-map-filters").first.wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        page.wait_for_function(
            """() => {
              const map = document.querySelector('.rules-presence-map.leaflet-container');
              return map !== null && map.querySelectorAll('img.leaflet-tile').length > 0;
            }""",
            timeout=_BROWSER_BOOT_TIMEOUT_MS,
        )
        marker = page.locator(".rules-presence-user-marker").first
        marker.hover()
        tooltip = page.locator(".rules-presence-map-hover-tooltip")
        tooltip.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        box = tooltip.bounding_box()
        assert box is not None
        assert box["width"] >= 120
        assert "Henrique" in tooltip.inner_text()
        assert "(" in tooltip.inner_text()
    finally:
        context.close()


@pytest.mark.browser
def test_users_tab_osm_tiles_are_visible(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Users tab must paint OSM tiles after async mount (not only markers)."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        _switch_to_users_tab(page)
        page.wait_for_function(
            """() => {
              const tile = document.querySelector('.rules-presence-map img.leaflet-tile');
              if (tile === null) return false;
              const rect = tile.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            }""",
            timeout=_BROWSER_BOOT_TIMEOUT_MS,
        )
    finally:
        context.close()


@pytest.mark.browser
def test_user_presence_map_shows_color_legend(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Status map shows a user/device legend when multiple locations are visible."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator(".rules-presence-map-filters").first.wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        legend = page.locator(".rules-presence-map-legend")
        legend.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        assert legend.locator(".rules-presence-map-legend-item").count() >= 2
        assert "Henrique" in legend.inner_text()
        assert page.locator(".rules-presence-map-filter-swatch").count() >= 2
    finally:
        context.close()


@pytest.mark.browser
def test_status_map_hover_tooltip_does_not_expand_dialog_scroll(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """User map tooltips must not widen/tall the Automations dialog body."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator(".rules-presence-map-filters").first.wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        body = page.locator(".rules-dialog-body")
        before = body.evaluate(
            """(el) => ({
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              scrollHeight: el.scrollHeight,
              clientHeight: el.clientHeight,
            })""",
        )
        marker = page.locator(".rules-presence-user-marker").first
        marker.hover()
        tooltip = page.locator(".rules-presence-map-hover-tooltip")
        tooltip.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        box = tooltip.bounding_box()
        assert box is not None
        assert box["width"] >= 120
        after = body.evaluate(
            """(el) => ({
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              scrollHeight: el.scrollHeight,
              clientHeight: el.clientHeight,
            })""",
        )
        assert after["scrollWidth"] <= before["scrollWidth"]
        assert after["scrollHeight"] <= before["scrollHeight"]
    finally:
        context.close()


@pytest.mark.browser
def test_users_tab_tooltip_not_clipped_at_map_edge(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Shell-hosted tooltips must stay visible while the map keeps overflow hidden."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        _switch_to_users_tab(page)
        users_map = _users_presence_map(page)
        users_map.locator("img.leaflet-tile").first.wait_for(
            state="visible",
            timeout=_BROWSER_BOOT_TIMEOUT_MS,
        )
        overflow = users_map.evaluate(
            "(el) => getComputedStyle(el).overflow",
        )
        assert overflow == "hidden"

        top_marker_y = float("inf")
        top_marker = None
        for marker in users_map.locator(".rules-presence-user-marker").all():
            box = marker.bounding_box()
            if box is None:
                continue
            if box["y"] < top_marker_y:
                top_marker_y = box["y"]
                top_marker = marker
        assert top_marker is not None
        top_marker.hover()
        tooltip = _users_presence_map_shell(page).locator(
            ".rules-presence-map-hover-tooltip.is-visible",
        )
        tooltip.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        hosted_on_shell = tooltip.evaluate(
            """(el) => {
              const map = el.closest('.rules-presence-map-shell')?.querySelector('.rules-presence-map');
              if (map === null || map === undefined) return false;
              return !map.contains(el);
            }""",
        )
        assert hosted_on_shell
        box = tooltip.bounding_box()
        assert box is not None
        assert box["width"] >= 120
        away_from_origin = tooltip.evaluate(
            """(el) => {
              const shell = el.closest('.rules-presence-map-shell');
              if (shell === null) return false;
              const t = el.getBoundingClientRect();
              const s = shell.getBoundingClientRect();
              return t.left > s.left + 24 || t.top > s.top + 24;
            }""",
        )
        assert away_from_origin
    finally:
        context.close()


@pytest.mark.browser
def test_user_tooltip_hides_when_pointer_leaves_marker(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Map-level hit testing must not leave a parked tooltip after the pointer moves away."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        _switch_to_users_tab(page)
        page.wait_for_function(
            """() => {
              const tooltip = document.querySelector('.rules-presence-map-hover-tooltip');
              return tooltip !== null && !tooltip.classList.contains('is-visible');
            }""",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        marker = page.locator(".rules-presence-user-marker").first
        marker.hover()
        tooltip = page.locator(".rules-presence-map-hover-tooltip.is-visible")
        tooltip.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)

        map_box = marker.evaluate(
            """(el) => {
              const map = el.closest('.rules-presence-map');
              if (map === null) return null;
              const rect = map.getBoundingClientRect();
              return {x: rect.x, y: rect.y, width: rect.width, height: rect.height};
            }""",
        )
        assert map_box is not None
        page.mouse.move(map_box["x"] + 12, map_box["y"] + 12)
        page.wait_for_function(
            """() => {
              const tooltip = document.querySelector('.rules-presence-map-hover-tooltip');
              return tooltip !== null && !tooltip.classList.contains('is-visible');
            }""",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
    finally:
        context.close()


@pytest.mark.browser
def test_conditions_tab_shows_astronomical_dynamic_cards(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Conditions tab lists sunrise, daylight, and sunset astronomical cards."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator("dialog.rules-dialog").wait_for(
            state="visible",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
        page.locator('.rules-tab[data-tab="conditions"]').click()
        page.wait_for_function(
            """() => {
              if (document.querySelector('.rules-tab[data-tab="conditions"].rules-tab-active') === null) {
                return false;
              }
              const labels = new Set(
                [...document.querySelectorAll('.rules-dialog-body .rules-dynamic-badge')].map(
                  (el) => el.textContent?.trim() ?? '',
                ),
              );
              return labels.has('Before sunrise (dynamic)')
                && labels.has('Daylight (dynamic)')
                && labels.has('After sunset (dynamic)');
            }""",
            timeout=_BROWSER_INTERACTION_TIMEOUT_MS,
        )
    finally:
        context.close()


@pytest.mark.browser
def test_conditions_home_location_link_opens_geofences_tab(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Sunset/sunrise cards link home location to the matching geofence row."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator('.rules-tab[data-tab="conditions"]').click()
        page.locator(".rules-inline-link").first.wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        assert page.locator(".rules-inline-link").first.inner_text() == "House"
        page.locator(".rules-inline-link").first.click()
        page.locator("#rules-geofence-map").wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        page.locator("tr.rules-geofence-row-focused").wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        assert page.locator('.rules-tab[data-tab="geofences"]').evaluate(
            "(el) => el.classList.contains('rules-tab-active')",
        )
    finally:
        context.close()


@pytest.mark.browser
def test_mail_tab_loads_smtp_settings_from_api(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Mail tab reads persisted SMTP config via GET /v1/settings/smtp."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator("dialog.rules-dialog").wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        with page.expect_request(
            lambda req: req.url.endswith("/v1/settings/smtp") and req.method == "GET",
        ) as smtp_get:
            page.locator('.rules-tab[data-tab="mail"]').click()
        response = smtp_get.value.response()
        assert response is not None
        assert response.status == 200
        page.locator(".rules-mail-form").wait_for(state="visible", timeout=_BROWSER_INTERACTION_TIMEOUT_MS)
        host_input = page.locator(".rules-mail-form input").first
        assert host_input.input_value() == "localhost"
    finally:
        context.close()


@pytest.mark.browser
def test_geofence_draw_mode_adds_crosshair_class(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Geofences tab draw toolbar toggles map crosshair mode."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_BROWSER_GOTO_TIMEOUT_MS)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator('.rules-tab[data-tab="geofences"]').click()
        page.locator("#rules-geofence-map").wait_for(state="visible", timeout=_BROWSER_BOOT_TIMEOUT_MS)
        page.get_by_role("button", name="Draw geofence").click()
        map_el = page.locator("#rules-geofence-map")
        assert "rules-geofence-draw-mode" in (map_el.get_attribute("class") or "")
    finally:
        context.close()
