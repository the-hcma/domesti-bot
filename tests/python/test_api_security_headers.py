"""Browser hardening headers (``_SecurityHeadersMiddleware``) and the landing-page CSP contract."""

from __future__ import annotations

import argparse
import re
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.api.app import _SecurityHeadersMiddleware, create_app

_STATIC_DIR = Path(__file__).resolve().parents[2] / "app" / "api" / "static"


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


def _client(tmp_path: Path) -> TestClient:
    args = argparse.Namespace(discovery_cache=str(tmp_path / "ui.sqlite"), tailwind_token=None)
    return TestClient(create_app(args))


def test_every_response_carries_nosniff_and_a_referrer_policy(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/health")
    assert response.status_code == HTTPStatus.OK
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_settings_responses_keep_no_store_next_to_the_hardening_headers(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/v1/settings/tailwind-token")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_landing_page_has_a_content_security_policy(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/")
    assert response.status_code == HTTPStatus.OK
    policy = response.headers["content-security-policy"]
    directives = {part.strip() for part in policy.split(";")}
    assert "script-src 'self'" in directives
    assert "frame-ancestors 'none'" in directives
    assert "object-src 'none'" in directives
    assert "base-uri 'self'" in directives
    assert "form-action 'self'" in directives
    # Deliberately no default-src: the page loads unpkg CSS, OSM tiles and LAN artwork, and uses inline styles.
    assert not any(d.startswith("default-src") for d in directives)


def test_the_same_ui_served_under_static_has_the_csp_too(tmp_path: Path) -> None:
    response = _client(tmp_path).get("/static/index.html")
    assert response.status_code == HTTPStatus.OK
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_the_dev_design_prototype_is_neither_served_nor_packaged(tmp_path: Path) -> None:
    """It runs an inline script, so it lives outside ``app/api/static`` instead of being exempt from the CSP."""
    client = _client(tmp_path)
    assert client.get("/static/compact-layout-prototype.html").status_code == HTTPStatus.NOT_FOUND
    assert not list(_STATIC_DIR.rglob("*prototype*"))
    assert (Path(__file__).resolve().parents[2] / "docs" / "prototypes" / "compact-layout-prototype.html").is_file()


def test_every_html_page_served_gets_the_csp_with_no_exemption(tmp_path: Path) -> None:
    client = _client(tmp_path)
    pages = list(_STATIC_DIR.rglob("*.html"))
    assert pages, "expected the landing page"
    for page in pages:
        response = client.get(f"/static/{page.relative_to(_STATIC_DIR).as_posix()}")
        assert response.status_code == HTTPStatus.OK
        assert "script-src 'self'" in response.headers["content-security-policy"], page.name


def test_json_api_responses_do_not_carry_the_html_csp(tmp_path: Path) -> None:
    assert "content-security-policy" not in _client(tmp_path).get("/health").headers


def test_middleware_does_not_override_headers_a_route_already_set() -> None:
    app = FastAPI()
    app.add_middleware(_SecurityHeadersMiddleware)

    @app.get("/probe")
    def probe() -> PlainTextResponse:
        return PlainTextResponse("ok", headers={"X-Content-Type-Options": "custom", "Referrer-Policy": "no-referrer"})

    response = TestClient(app).get("/probe")
    assert response.headers["x-content-type-options"] == "custom"
    assert response.headers["referrer-policy"] == "no-referrer"


def _served_html_pages() -> list[Path]:
    return sorted(_STATIC_DIR.rglob("*.html"))


def test_served_html_pages_have_no_inline_script_or_event_handlers() -> None:
    """``script-src 'self'`` would block these, so a CSP-protected page must not rely on them."""
    pages = _served_html_pages()
    assert _STATIC_DIR / "index.html" in pages
    for page in pages:
        html = page.read_text(encoding="utf-8")
        inline_scripts = re.findall(r"<script(?![^>]*(?<![\w-])src\s*=)[^>]*>", html, flags=re.IGNORECASE)
        assert inline_scripts == [], f"{page.name}: inline script"
        handlers = re.findall(r"\son[a-z]+\s*=", html, flags=re.IGNORECASE)
        assert handlers == [], f"{page.name}: event handler attributes {handlers}"
        assert "javascript:" not in html.lower(), f"{page.name}: javascript: URL"


def test_the_inline_script_pattern_catches_the_cases_it_is_meant_to() -> None:
    pattern = r"<script(?![^>]*(?<![\w-])src\s*=)[^>]*>"
    assert re.findall(pattern, "<SCRIPT>alert(1)</SCRIPT>", flags=re.IGNORECASE)
    assert re.findall(pattern, '<script type="module" data-src="x.js">', flags=re.IGNORECASE)
    assert not re.findall(pattern, '<script type="module" src="/static/dist/main.js"></script>', flags=re.IGNORECASE)
    assert not re.findall(pattern, '<script src = "/a.js"></script>', flags=re.IGNORECASE)
