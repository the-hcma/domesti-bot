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


def _channels(color: str) -> tuple[float, float, float, float]:
    """``(r, g, b, alpha)`` of a computed ``rgb()`` / ``rgba()`` color."""

    numbers = [float(part) for part in re.findall(r"[\d.]+", color)]
    assert len(numbers) in (3, 4), f"Expected an rgb() or rgba() color, got {color!r}"
    return numbers[0], numbers[1], numbers[2], numbers[3] if len(numbers) == 4 else 1.0


def _contrast_ratio(foreground: str, background: str) -> float:
    """WCAG contrast ratio of ``foreground`` over an opaque ``background`` (alpha is composited)."""

    def _luminance(rgb: tuple[float, float, float]) -> float:
        linear = [(c / 255) / 12.92 if c / 255 <= 0.04045 else (((c / 255) + 0.055) / 1.055) ** 2.4 for c in rgb]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    back_r, back_g, back_b, back_alpha = _channels(background)
    assert back_alpha == 1.0, f"Expected an opaque background, got {background!r}"
    fore_r, fore_g, fore_b, fore_alpha = _channels(foreground)
    blended = (
        fore_r * fore_alpha + back_r * (1 - fore_alpha),
        fore_g * fore_alpha + back_g * (1 - fore_alpha),
        fore_b * fore_alpha + back_b * (1 - fore_alpha),
    )
    lighter, darker = sorted((_luminance(blended), _luminance((back_r, back_g, back_b))), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def test_contrast_ratio_matches_known_wcag_values() -> None:
    assert _contrast_ratio("rgb(0, 0, 0)", "rgb(255, 255, 255)") == pytest.approx(21.0)
    assert _contrast_ratio("rgb(255, 255, 255)", "rgb(255, 255, 255)") == pytest.approx(1.0)


def test_contrast_ratio_composites_a_translucent_foreground_and_rejects_a_translucent_background() -> None:
    # 50% white over black is mid grey (127.5): the ratio sits between black-on-black and white-on-black.
    assert 1.0 < _contrast_ratio("rgba(255, 255, 255, 0.5)", "rgb(0, 0, 0)") < 21.0
    with pytest.raises(AssertionError, match="opaque background"):
        _contrast_ratio("rgb(255, 255, 255)", "rgba(0, 0, 0, 0)")


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


def _new_context(browser: Any, *, os_scheme: str, stored_theme: str | None) -> Any:
    """A browser context whose OS color scheme is ``os_scheme`` and whose saved page theme is ``stored_theme``.

    A saved theme sets ``data-theme`` on the page (``html[data-theme=...]`` rules); without one the page follows
    the OS scheme (``html:not([data-theme])`` rules). Both paths must stay readable.
    """

    context = browser.new_context(viewport={"width": 1280, "height": 800}, color_scheme=os_scheme)
    if stored_theme is not None:
        context.add_init_script(f"window.localStorage.setItem('domesti-color-theme', '{stored_theme}')")
    return context


# (OS scheme, saved theme): the OS scheme alone, and a saved theme that disagrees with it.
_THEME_CASES = [("light", None), ("dark", None), ("dark", "light"), ("light", "dark")]


@pytest.mark.browser
@pytest.mark.parametrize(("scheme", "stored_theme"), _THEME_CASES)
def test_operator_alert_message_is_readable_in_both_color_schemes(
    chromium_browser: Any,
    landing_base_url: str,
    smtp_alert: None,
    scheme: str,
    stored_theme: str | None,
) -> None:
    context = _new_context(chromium_browser, os_scheme=scheme, stored_theme=stored_theme)
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
            page.screenshot(path=str(Path(shot_dir) / f"operator-alert-{scheme}-{stored_theme or 'os'}.png"))
        # The message must not fall back to the muted paragraph color: it is the banner's own color.
        label = f"{scheme}, saved theme {stored_theme}"
        assert message_color == banner_color, f"message {message_color} != banner {banner_color} ({label})"
        ratio = _contrast_ratio(message_color, background)
        assert ratio >= _MIN_CONTRAST_RATIO, f"contrast {ratio:.2f}:1 of {message_color} on {background} ({label})"
    finally:
        context.close()


@pytest.mark.browser
@pytest.mark.parametrize(("scheme", "stored_theme"), _THEME_CASES)
@pytest.mark.parametrize("kind", ["missing", "returned"])
def test_presence_notice_message_and_buttons_are_readable_in_both_color_schemes(
    chromium_browser: Any,
    landing_base_url: str,
    scheme: str,
    stored_theme: str | None,
    kind: str,
) -> None:
    mac = "5c:e9:31:60:c5:bd"
    device_presence_store.note_disappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    if kind == "returned":
        device_presence_store.note_reappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    context = _new_context(chromium_browser, os_scheme=scheme, stored_theme=stored_theme)
    page = context.new_page()
    label = f"{kind}, {scheme}, saved theme {stored_theme}"
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_GOTO_TIMEOUT_MS)
        banner = page.locator(f".presence-notice-{kind}").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        background = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        banner_color = banner.evaluate("el => getComputedStyle(el).color")
        message = banner.locator(".presence-notice-message")
        assert message.evaluate("el => getComputedStyle(el).color") == banner_color, label
        for part in (message, banner.locator("button").first):
            color = part.evaluate("el => getComputedStyle(el).color")
            ratio = _contrast_ratio(color, background)
            assert ratio >= _MIN_CONTRAST_RATIO, f"contrast {ratio:.2f}:1 of {color} on {background} ({label})"
        shot_dir = os.environ.get("DOMESTI_ALERT_UI_SCREENSHOT_DIR")
        if shot_dir:
            page.screenshot(path=str(Path(shot_dir) / f"presence-{kind}-{scheme}-{stored_theme or 'os'}.png"))
    finally:
        context.close()


def _button_surface(button: Any, banner_background: str) -> str:
    """The opaque color a button's own background paints over the banner (its translucent fill composited)."""

    fill = button.evaluate("el => getComputedStyle(el).backgroundColor")
    red, green, blue, alpha = _channels(fill)
    back_r, back_g, back_b, _ = _channels(banner_background)
    return (
        f"rgb({red * alpha + back_r * (1 - alpha):.0f}, "
        f"{green * alpha + back_g * (1 - alpha):.0f}, "
        f"{blue * alpha + back_b * (1 - alpha):.0f})"
    )


@pytest.mark.browser
@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("kind", ["missing", "returned"])
def test_presence_notice_button_stays_readable_on_hover_and_keyboard_focus(
    chromium_browser: Any,
    landing_base_url: str,
    scheme: str,
    kind: str,
) -> None:
    mac = "5c:e9:31:60:c5:bd"
    device_presence_store.note_disappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    if kind == "returned":
        device_presence_store.note_reappeared(device_id=mac, family_id="kasa", host=None, label="Master bedroom fan")
    context = _new_context(chromium_browser, os_scheme=scheme, stored_theme=None)
    page = context.new_page()
    try:
        page.goto(landing_base_url, wait_until="networkidle", timeout=_GOTO_TIMEOUT_MS)
        banner = page.locator(f".presence-notice-{kind}").first
        banner.wait_for(timeout=_BOOT_TIMEOUT_MS)
        banner_background = banner.evaluate("el => getComputedStyle(el).backgroundColor")
        button = banner.locator("button").first

        button.hover()
        color = button.evaluate("el => getComputedStyle(el).color")
        ratio = _contrast_ratio(color, _button_surface(button, banner_background))
        assert ratio >= _MIN_CONTRAST_RATIO, f"hover contrast {ratio:.2f}:1 ({kind}, {scheme})"

        page.mouse.move(0, 0)
        for _ in range(40):  # Tab until the button has keyboard focus (condition-based, bounded)
            page.keyboard.press("Tab")
            if button.evaluate("el => el === document.activeElement"):
                break
        assert button.evaluate("el => el === document.activeElement"), "could not reach the button by keyboard"
        assert button.evaluate("el => el.matches(':focus-visible')")
        ratio = _contrast_ratio(color, _button_surface(button, banner_background))
        assert ratio >= _MIN_CONTRAST_RATIO, f"focus contrast {ratio:.2f}:1 ({kind}, {scheme})"
        # The tinted background is not the only focus cue: there is a visible outline too.
        assert button.evaluate("el => getComputedStyle(el).outlineStyle") != "none"
    finally:
        context.close()
