"""Rules hub UI — CSS contract and desktop browser smoke (mock data)."""

from __future__ import annotations

import argparse
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INDEX_HTML_PATH = _REPO_ROOT / "app" / "api" / "static" / "index.html"
_MAIN_JS_PATH = _REPO_ROOT / "app" / "api" / "static" / "dist" / "main.js"


async def _bootstrap_empty_device_state(
    *_args: Any,
    **_kwargs: Any,
) -> DeviceManagersState:
    """Skip LAN discovery so browser tests can render the desktop shell."""

    kasa_mgr = MagicMock(spec=KasaDeviceManager)
    kasa_mgr.switches = ()
    return DeviceManagersState(
        kasa_mgr=kasa_mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )


def test_index_html_includes_rules_hub_css() -> None:
    """Landing-page CSS defines Rules hub chrome (tabs, mock pill, geofence map)."""

    html = _INDEX_HTML_PATH.read_text(encoding="utf-8")
    for needle in (
        "rules-mock-pill",
        "rules-tab-bar",
        "rules-geofence-map",
        "rules-geofence-draw-mode",
        "rules-dynamic-badge",
        "rules-condition-list",
        "rules-device-action-group",
        "rules-enable-toggle",
        "rules-info-badge",
        "rules-inline-link",
        "rules-geofence-row-focused",
        "rules-info-popover[hidden]",
        "rules-mail-test-row",
        "rules-presence-map",
        "rules-presence-map-filters",
        "rules-day-shortcuts",
        "automations-dialog .leaflet-tooltip",
        "color-scheme: dark",
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
def landing_base_url() -> Iterator[str]:
    """Serve the FastAPI app on an OS-allocated loopback port (bundle must exist)."""

    if not _MAIN_JS_PATH.is_file():
        pytest.skip("app/api/static/dist/main.js missing — run pnpm run build in web/")

    args = argparse.Namespace(
        discovery_cache=None,
        tailwind_token=None,
    )
    with patch(
        "app.api.app.bootstrap_device_managers",
        _bootstrap_empty_device_state,
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
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_selector("#app", timeout=15_000)
        assert page.locator(".app-menu").count() == 0
    finally:
        context.close()


@pytest.mark.browser
def test_rules_hub_opens_with_mock_seed_rule(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Desktop ☰ → Automations opens the hub and shows the seeded arrive-home rule."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_selector(".app-menu", timeout=15_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=10_000)
        mock_pill = page.locator(".rules-mock-pill")
        assert mock_pill.is_visible()
        assert mock_pill.inner_text().lower() == "mock rules"
        assert "Automations" in dialog.inner_text()
        assert "Rules" in dialog.inner_text()
        assert "Welcome home" in dialog.inner_text()
        page.locator('.rules-tab[data-tab="rules"]').click()
        assert "Add rule" in dialog.inner_text()
    finally:
        context.close()


@pytest.mark.browser
def test_participant_presence_map_renders_osm_tiles_with_filters(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Status and Participants tabs share one filtered presence map with zoom controls."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=10_000)
        page.locator(".rules-presence-map-filters").wait_for(state="visible", timeout=10_000)
        page.wait_for_function(
            """() => {
              const map = document.querySelector('.rules-presence-map.leaflet-container');
              return map !== null && map.querySelectorAll('img.leaflet-tile').length > 0;
            }""",
            timeout=15_000,
        )
        assert page.locator(".rules-presence-map-filter").count() >= 2
        assert page.locator(".leaflet-control-zoom").count() >= 1

        page.locator('.rules-tab[data-tab="participants"]').click()
        page.locator(".rules-presence-map-filters").wait_for(state="visible", timeout=10_000)
        page.wait_for_function(
            """() => {
              const map = document.querySelector('.rules-presence-map.leaflet-container');
              return map !== null && map.querySelectorAll('img.leaflet-tile').length > 0;
            }""",
            timeout=15_000,
        )
        marker = page.locator(".rules-presence-participant-marker").first
        marker.hover()
        tooltip = page.locator(".rules-presence-map-tooltip")
        tooltip.wait_for(state="visible", timeout=5_000)
        assert "Henrique" in tooltip.inner_text()
        assert "(" in tooltip.inner_text()
    finally:
        context.close()


@pytest.mark.browser
def test_status_map_hover_tooltip_does_not_expand_dialog_scroll(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Participant map tooltips must not widen/tall the Automations dialog body."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator(".rules-presence-map-filters").wait_for(state="visible", timeout=10_000)
        body = page.locator(".rules-dialog-body")
        before = body.evaluate(
            """(el) => ({
              scrollWidth: el.scrollWidth,
              clientWidth: el.clientWidth,
              scrollHeight: el.scrollHeight,
              clientHeight: el.clientHeight,
            })""",
        )
        marker = page.locator(".rules-presence-participant-marker").first
        marker.hover()
        page.locator(".rules-presence-map-tooltip").wait_for(state="visible", timeout=5_000)
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
def test_conditions_home_location_link_opens_geofences_tab(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    """Sunset/sunrise cards link home location to the matching geofence row."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator('.rules-tab[data-tab="conditions"]').click()
        page.locator(".rules-inline-link").first.wait_for(state="visible", timeout=10_000)
        assert page.locator(".rules-inline-link").first.inner_text() == "House"
        page.locator(".rules-inline-link").first.click()
        page.locator("#rules-geofence-map").wait_for(state="visible", timeout=10_000)
        page.locator("tr.rules-geofence-row-focused").wait_for(state="visible", timeout=10_000)
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
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator("dialog.rules-dialog").wait_for(state="visible", timeout=10_000)
        with page.expect_request(
            lambda req: req.url.endswith("/v1/settings/smtp") and req.method == "GET",
        ) as smtp_get:
            page.locator('.rules-tab[data-tab="mail"]').click()
        response = smtp_get.value.response()
        assert response is not None
        assert response.status == 200
        page.locator(".rules-mail-form").wait_for(state="visible", timeout=10_000)
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
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Automations").click()
        page.locator('.rules-tab[data-tab="geofences"]').click()
        page.locator("#rules-geofence-map").wait_for(state="visible", timeout=15_000)
        page.get_by_role("button", name="Draw geofence").click()
        map_el = page.locator("#rules-geofence-map")
        assert "rules-geofence-draw-mode" in (map_el.get_attribute("class") or "")
    finally:
        context.close()

