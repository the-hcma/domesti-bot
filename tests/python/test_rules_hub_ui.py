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
    """☰ menu (and Rules entry) is desktop-only — absent at phone widths."""

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
    """Desktop ☰ → Rules opens the hub and shows the seeded arrive-home rule."""

    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800})
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=30_000)
        page.wait_for_selector(".app-menu", timeout=15_000)
        page.locator(".btn-menu").click()
        page.get_by_role("menuitem", name="Rules").click()
        dialog = page.locator("dialog.rules-dialog")
        dialog.wait_for(state="visible", timeout=10_000)
        assert page.locator(".rules-mock-pill").is_visible()
        assert "Welcome home" in dialog.inner_text()
    finally:
        context.close()

