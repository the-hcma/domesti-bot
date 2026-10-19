"""Banner readability in the real landing page (domesti-bot#875).

Serves the FastAPI app on an OS-allocated loopback port with a stubbed device state and checks, in
headless Chromium and in both color schemes, that the operator alert and the device presence
notices draw their message in the banner's own text color and meet the WCAG AA contrast ratio
against the banner background. Skipped without Playwright or ``app/api/static/dist/main.js``.
"""

from __future__ import annotations

import argparse
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
import uvicorn

from app.api.app import create_app
from app.device_presence import device_presence_store
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.operator_alerts import operator_alert_store

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_BUNDLE = _REPO_ROOT / "automation-rules.json.example"
_MAIN_JS_PATH = _REPO_ROOT / "app" / "api" / "static" / "dist" / "main.js"

_ALERT_MESSAGE = "SMTP delivery failed: connection refused by smtp.example.com"
_BOOT_TIMEOUT_MS = 15_000
_GOTO_TIMEOUT_MS = 30_000
# WCAG 2.x level AA for normal-size text.
_MIN_CONTRAST_RATIO = 4.5


def _contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio between two computed ``rgb()`` / ``rgba()`` colors."""

    def _luminance(color: str) -> float:
        channels = [int(part) for part in re.findall(r"\d+", color)[:3]]
        linear = [(c / 255) / 12.92 if c / 255 <= 0.03928 else (((c / 255) + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    lighter, darker = sorted((_luminance(foreground), _luminance(background)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_ratio_matches_known_wcag_values() -> None:
    assert _contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(21.0)
    assert _contrast_ratio("rgb(255, 255, 255)", "rgb(255, 255, 255)") == pytest.approx(1.0)


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def landing_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    if not _MAIN_JS_PATH.is_file():
        pytest.skip("app/api/static/dist/main.js missing — run pnpm run build in web/")
    cache_path = tmp_path_factory.mktemp("operator-alert-ui") / "discovery.sqlite"
    kasa = MagicMock(spec=KasaDeviceManager)
    type(kasa).switches = PropertyMock(return_value=())

    async def bootstrap_device_state(*_args: Any, **_kwargs: Any) -> DeviceManagersState:
        return DeviceManagersState(
            kasa_mgr=kasa,
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=cache_path,
            args=argparse.Namespace(),
        )

    args = argparse.Namespace(discovery_cache=str(cache_path), tailwind_token=None)
    with (
        patch("app.api.app.bootstrap_device_managers", bootstrap_device_state),
        patch.dict(os.environ, {"DOMESTI_AUTOMATION_RULES_FILE": str(_EXAMPLE_BUNDLE)}, clear=False),
    ):
        app = create_app(args)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10.0
        while not server.started:
            if time.monotonic() > deadline:
                pytest.fail("uvicorn did not start within 10s")
            time.sleep(0.02)
        sockets = server.servers[0].sockets
        assert sockets is not None and len(sockets) > 0
        yield f"http://127.0.0.1:{sockets[0].getsockname()[1]}"
        server.should_exit = True
        thread.join(timeout=5.0)


@pytest.fixture
def smtp_alert() -> Iterator[None]:
    operator_alert_store.record_smtp_notification_failure(message=_ALERT_MESSAGE, reason_code="smtp_delivery_failed")
    yield
    operator_alert_store.clear_smtp_notification_failure()


@pytest.mark.browser
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_operator_alert_message_is_readable_in_both_color_schemes(
    chromium_browser: Any,
    landing_base_url: str,
    smtp_alert: None,
    scheme: str,
) -> None:
    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme)
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_GOTO_TIMEOUT_MS)
        banner = page.locator(".operator-alert-banner").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        message = banner.locator(".operator-alert-message")
        assert _ALERT_MESSAGE in message.inner_text()
        banner_color = banner.evaluate("el => getComputedStyle(el).color")
        message_color = message.evaluate("el => getComputedStyle(el).color")
        background = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        shot_dir = os.environ.get("DOMESTI_ALERT_UI_SCREENSHOT_DIR")
        if shot_dir:
            page.screenshot(path=str(Path(shot_dir) / f"operator-alert-{scheme}.png"))
        # The message must not fall back to the muted paragraph color: it is the banner's own color.
        assert message_color == banner_color, f"message {message_color} != banner {banner_color} ({scheme})"
        ratio = _contrast_ratio(message_color, background)
        assert ratio >= _MIN_CONTRAST_RATIO, f"contrast {ratio:.2f}:1 of {message_color} on {background} ({scheme})"
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["missing", "returned"])
def test_presence_notice_message_and_buttons_are_readable_in_both_color_schemes(
    chromium_browser: Any,
    landing_base_url: str,
    scheme: str,
    kind: str,
) -> None:
    mac = "5c:e9:31:60:c5:bd"
    device_presence_store.note_disappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    if kind == "returned":
        device_presence_store.note_reappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    context = chromium_browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=scheme)
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_GOTO_TIMEOUT_MS)
        banner = page.locator(f".presence-notice-{kind}").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        background = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        for part in (banner.locator(".presence-notice-message"), banner.locator("button").first):
            color = part.evaluate("el => getComputedStyle(el).color")
            ratio = _contrast_ratio(color, background)
            assert ratio >= _MIN_CONTRAST_RATIO, f"contrast {ratio:.2f}:1 of {color} on {background} ({kind}, {scheme})"
        shot_dir = os.environ.get("DOMESTI_ALERT_UI_SCREENSHOT_DIR")
        if shot_dir:
            page.screenshot(path=str(Path(shot_dir) / f"presence-{kind}-{scheme}.png"))
    finally:
        context.close()
