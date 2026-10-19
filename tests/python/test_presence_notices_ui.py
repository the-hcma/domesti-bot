"""Device presence notices in the real landing page (live API, headless Chromium; domesti-bot#867).

Serves the FastAPI app on an OS-allocated loopback port with a stubbed device state, seeds the
process-wide presence store, and drives the compiled bundle: banners, the three actions, the
confirmation before forgetting, the toast for a notice that appears while the page is open, and the
phone-width layout. Skipped without Playwright or ``app/api/static/dist/main.js``.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
import uvicorn

from app.api.app import create_app
from app.device_enums import DevicePresenceDecision
from app.device_presence import device_presence_store
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_BUNDLE = _REPO_ROOT / "automation-rules.json.example"
_MAIN_JS_PATH = _REPO_ROOT / "app" / "api" / "static" / "dist" / "main.js"

_FAN_MAC = "5c:e9:31:60:c5:bd"
_FAN_LABEL = "Master bedroom fan"
_LAMP_MAC = "aa:bb:cc:dd:ee:02"
_LAMP_LABEL = "Visitor lamp"

_BOOT_TIMEOUT_MS = 15_000
_GOTO_TIMEOUT_MS = 30_000
# The page polls /v1/ui/state every few seconds; a notice raised after load shows up on a later poll.
_POLL_WAIT_MS = 20_000


@pytest.fixture(scope="module")
def chromium_browser() -> Iterator[Any]:
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture(scope="module")
def kasa_manager() -> MagicMock:
    manager = MagicMock(spec=KasaDeviceManager)
    type(manager).switches = PropertyMock(return_value=())
    manager.forget_device = AsyncMock(return_value=True)
    return manager


@pytest.fixture(scope="module")
def landing_base_url(tmp_path_factory: pytest.TempPathFactory, kasa_manager: MagicMock) -> Iterator[str]:
    if not _MAIN_JS_PATH.is_file():
        pytest.skip("app/api/static/dist/main.js missing — run pnpm run build in web/")
    cache_path = tmp_path_factory.mktemp("presence-notices-ui") / "discovery.sqlite"
    args = argparse.Namespace(discovery_cache=str(cache_path), tailwind_token=None)

    async def bootstrap_device_state(*_args: Any, **_kwargs: Any) -> DeviceManagersState:
        return DeviceManagersState(
            kasa_mgr=kasa_manager,
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=cache_path,
            args=argparse.Namespace(),
        )

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


def _missing(mac: str = _FAN_MAC, label: str = _FAN_LABEL) -> str:
    notice = device_presence_store.note_disappeared(device_id=mac, family_id="kasa", host="192.168.86.167", label=label)
    assert notice is not None
    return notice.notice_id


def _returned(mac: str = _FAN_MAC, label: str = _FAN_LABEL) -> str:
    _missing(mac, label)
    notice = device_presence_store.note_reappeared(device_id=mac, family_id="kasa", host="192.168.86.167", label=label)
    assert notice is not None
    return notice.notice_id


def _open(page: Any, base_url: str) -> None:
    page.goto(base_url, wait_until="networkidle", timeout=_GOTO_TIMEOUT_MS)
    page.wait_for_selector("#app", timeout=_BOOT_TIMEOUT_MS)


def _new_page(browser: Any, *, width: int = 1280, height: int = 800) -> tuple[Any, Any]:
    context = browser.new_context(viewport={"width": width, "height": height})
    return context, context.new_page()


@pytest.mark.browser
def test_missing_device_shows_a_red_alert_banner_with_keep_checking_and_forget(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    _missing()
    context, page = _new_page(chromium_browser)
    try:
        _open(page, landing_base_url)
        banner = page.locator(".presence-notice-missing").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        assert banner.get_attribute("role") == "alert"
        text = banner.inner_text()
        assert _FAN_LABEL in text and _FAN_MAC in text and "stopped responding" in text
        assert [b.inner_text() for b in banner.locator("button").all()] == ["Keep checking", "Forget it"]
        # The message must be as readable as the buttons: white on the banner, not the muted paragraph color.
        message_color = banner.locator(".presence-notice-message").evaluate("el => getComputedStyle(el).color")
        banner_color = banner.evaluate("el => getComputedStyle(el).color")
        assert message_color == banner_color == "rgb(255, 255, 255)"
        assert page.locator(".presence-notice-returned").count() == 0
    finally:
        context.close()


@pytest.mark.browser
def test_returned_device_shows_a_green_status_banner_with_dismiss_that_clears_it(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    notice_id = _returned()
    context, page = _new_page(chromium_browser)
    try:
        _open(page, landing_base_url)
        banner = page.locator(".presence-notice-returned").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        assert banner.get_attribute("role") == "status"
        assert "responding again" in banner.inner_text()
        missing_bg = page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--danger')")
        returned_bg = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        assert missing_bg.strip() != "" and returned_bg not in ("", "rgba(0, 0, 0, 0)")
        banner.get_by_role("button", name="Dismiss").click()
        page.wait_for_selector(".presence-notice", state="detached", timeout=_BOOT_TIMEOUT_MS)
        assert device_presence_store.get(notice_id) is None
    finally:
        context.close()


@pytest.mark.browser
def test_keep_checking_hides_the_banner_and_records_the_decision(
    chromium_browser: Any,
    landing_base_url: str,
    kasa_manager: MagicMock,
) -> None:
    notice_id = _missing()
    kasa_manager.forget_device.reset_mock()
    context, page = _new_page(chromium_browser)
    try:
        _open(page, landing_base_url)
        page.get_by_role("button", name="Keep checking").click()
        page.wait_for_selector(".presence-notice", state="detached", timeout=_BOOT_TIMEOUT_MS)
        page.wait_for_selector(".action-toast-success", timeout=_BOOT_TIMEOUT_MS)
        assert f"Will keep checking for {_FAN_LABEL}" in page.locator(".action-toast-success").inner_text()
        stored = device_presence_store.get(notice_id)
        assert stored is not None and stored.decision == DevicePresenceDecision.KEEP_CHECKING
        kasa_manager.forget_device.assert_not_awaited()
    finally:
        context.close()


@pytest.mark.browser
def test_forget_asks_for_confirmation_and_only_then_removes_the_device(
    chromium_browser: Any,
    landing_base_url: str,
    kasa_manager: MagicMock,
) -> None:
    notice_id = _missing()
    kasa_manager.forget_device.reset_mock()
    context, page = _new_page(chromium_browser)
    try:
        _open(page, landing_base_url)
        page.get_by_role("button", name="Forget it").click()
        dialog = page.locator("dialog.ui-confirm-dialog")
        dialog.wait_for(timeout=_BOOT_TIMEOUT_MS)
        assert _FAN_LABEL in dialog.inner_text() and "name and settings are kept" in dialog.inner_text()

        dialog.get_by_role("button", name="Keep it").click()
        page.wait_for_selector("dialog.ui-confirm-dialog", state="detached", timeout=_BOOT_TIMEOUT_MS)
        assert page.locator(".presence-notice-missing").count() == 1
        kasa_manager.forget_device.assert_not_awaited()

        page.get_by_role("button", name="Forget it").click()
        page.locator("dialog.ui-confirm-dialog").get_by_role("button", name="Forget", exact=True).click()
        page.wait_for_selector(".presence-notice", state="detached", timeout=_BOOT_TIMEOUT_MS)
        page.wait_for_selector(".action-toast-success", timeout=_BOOT_TIMEOUT_MS)
        assert f"Forgot {_FAN_LABEL}" in page.locator(".action-toast-success").inner_text()
        kasa_manager.forget_device.assert_awaited_once_with(_FAN_MAC)
        assert device_presence_store.get(notice_id) is None
    finally:
        context.close()


@pytest.mark.browser
def test_notice_raised_after_load_shows_up_with_an_error_toast(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    context, page = _new_page(chromium_browser)
    try:
        _open(page, landing_base_url)
        assert page.locator(".presence-notice").count() == 0
        _missing(_LAMP_MAC, _LAMP_LABEL)
        page.wait_for_selector(".presence-notice-missing", timeout=_POLL_WAIT_MS)
        page.wait_for_selector(".action-toast-error", timeout=_POLL_WAIT_MS)
        assert _LAMP_LABEL in page.locator(".action-toast-error").inner_text()
    finally:
        context.close()


@pytest.mark.browser
def test_banners_fit_a_phone_viewport_without_horizontal_scroll(
    chromium_browser: Any,
    landing_base_url: str,
) -> None:
    _missing()
    _returned(_LAMP_MAC, _LAMP_LABEL)
    context, page = _new_page(chromium_browser, width=390, height=844)
    try:
        _open(page, landing_base_url)
        page.wait_for_selector(".presence-notice-returned", timeout=_BOOT_TIMEOUT_MS)
        assert page.locator(".presence-notice").count() == 2
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        for banner in page.locator(".presence-notice").all():
            box = banner.bounding_box()
            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 390
            for button in banner.locator("button").all():
                button_box = button.bounding_box()
                assert button_box is not None and button_box["x"] + button_box["width"] <= 390
        shot_dir = os.environ.get("DOMESTI_PRESENCE_UI_SCREENSHOT_DIR")
        if shot_dir:
            page.screenshot(path=str(Path(shot_dir) / "presence-notices-phone.png"))
            page.set_viewport_size({"width": 1280, "height": 800})
            page.screenshot(path=str(Path(shot_dir) / "presence-notices-desktop.png"))
    finally:
        context.close()
