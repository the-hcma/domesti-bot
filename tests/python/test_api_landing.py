"""Tests for :mod:`app.api.app` static routes and deferred-discovery contract.

These tests cover:

* the friendly landing page at ``/``;
* the silent ``/favicon.ico`` short-circuit;
* the discovery-aware ``/health`` payload;
* the ``503 Retry-After`` contract used by protected routes while device
  discovery is still in flight;
* the lifespan contract that the HTTP server becomes ready as soon as the
  ASGI lifespan yields, *without* waiting for ``bootstrap_device_managers``
  to finish.

The discovery lifespan goes out to the LAN, so most tests intentionally do
*not* use ``with TestClient(...)``. They build an app, talk to it directly,
and mutate ``app.state`` to simulate the various discovery states. One
test (``test_lifespan_yields_immediately_even_when_discovery_blocks``) does
exercise the real lifespan path with a stubbed ``bootstrap_device_managers``
to prove the non-blocking behavior end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    # Do NOT use ``with TestClient(...)``: that would run the discovery
    # lifespan, which goes out to the LAN. The static routes under test
    # don't depend on ``app.state.device_state``, so we can issue requests
    # directly without lifespan startup.
    return TestClient(app), app


def test_favicon_returns_204_no_content() -> None:
    client, _app = _client()
    response = client.get("/favicon.ico")
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert response.content == b""


def test_health_reports_discovery_failed_when_error_set() -> None:
    client, app = _client()
    app.state.device_state = None
    app.state.discovery_error = "RuntimeError('no LAN')"
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "domesti-bot"
    assert payload["ready"] is False
    assert payload["discovery"] == "failed"
    assert payload["error"] == "RuntimeError('no LAN')"


def test_health_reports_discovery_in_progress_before_state_is_set() -> None:
    client, _app = _client()
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "domesti-bot"
    assert payload["ready"] is False
    assert payload["discovery"] == "in_progress"
    assert payload["error"] is None


def test_health_reports_ready_when_state_is_set() -> None:
    client, app = _client()
    app.state.device_state = object()  # marker, not a real state
    app.state.discovery_error = None
    response = client.get("/health")
    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    assert payload["ready"] is True
    assert payload["discovery"] == "ready"
    assert payload["error"] is None


def test_lifespan_yields_immediately_even_when_discovery_blocks() -> None:
    """Lifespan startup must not wait on ``bootstrap_device_managers``."""

    async def _slow_bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        # Simulate a discovery sweep that never finishes within the test.
        # ``asyncio.sleep`` raises ``CancelledError`` cleanly when the
        # lifespan cancels the background task on shutdown.
        await asyncio.sleep(60)
        return None

    args = argparse.Namespace()
    app = create_app(args)
    with patch("app.api.app.bootstrap_device_managers", _slow_bootstrap):
        started = time.perf_counter()
        with TestClient(app) as client:
            startup_elapsed = time.perf_counter() - started
            # If discovery were on the critical path the slow_bootstrap above
            # would block startup for ~60s. A generous 5s upper bound proves
            # the lifespan returned without waiting.
            assert startup_elapsed < 5.0, (
                f"lifespan startup took {startup_elapsed:.1f}s; discovery is still blocking"
            )
            # Static routes must be live immediately:
            assert client.get("/").status_code == HTTPStatus.OK
            assert client.get("/favicon.ico").status_code == HTTPStatus.NO_CONTENT
            health = client.get("/health").json()
            assert health["ready"] is False
            assert health["discovery"] == "in_progress"
            # Protected routes must surface a 503 with Retry-After:
            r = client.get("/v1/completion-aliases")
            assert r.status_code == HTTPStatus.SERVICE_UNAVAILABLE
            assert r.headers.get("Retry-After") == "2"
            assert "in progress" in r.json()["detail"].lower()


def test_protected_route_returns_503_with_failure_detail_when_discovery_failed() -> None:
    client, app = _client()
    app.state.device_state = None
    app.state.discovery_error = "OSError('no LAN')"
    response = client.get("/v1/completion-aliases")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers.get("Retry-After") == "30"
    assert "Device discovery failed" in response.json()["detail"]
    assert "OSError" in response.json()["detail"]


def test_protected_route_returns_503_with_retry_after_while_discovery_in_progress() -> None:
    client, _app = _client()
    response = client.get("/v1/completion-aliases")
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers.get("Retry-After") == "2"
    detail = response.json()["detail"]
    assert "in progress" in detail.lower()


def test_root_landing_page_includes_app_root_for_tile_ui() -> None:
    """``main.ts`` mounts the tile grid into ``#app``; the container must
    exist before the bundle runs so the controller's ``getElementById``
    succeeds."""

    client, _app = _client()
    body = client.get("/").text
    assert 'id="app"' in body


def test_root_landing_page_includes_js_boot_hint_before_hydration() -> None:
    """When ``/static/dist/main.js`` is missing (404), the HTML still explains why
    the page stayed empty and points at ``/health`` plus the bundle build path."""

    client, _app = _client()
    body = client.get("/").text
    assert 'id="app-js-boot-hint"' in body
    assert "app/api/static/dist/main.js" in body
    assert "/health" in body


def test_root_landing_page_is_clean_html_without_admin_chrome() -> None:
    """Landing page is the tile UI only — no banner, no endpoints list.

    Admin endpoints (``/health``, ``/v1/...``) intentionally live in
    ``/openapi.json`` rather than user-facing HTML.
    """
    client, _app = _client()
    response = client.get("/")
    assert response.status_code == HTTPStatus.OK
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("text/html"), ctype
    body = response.text
    assert "<title>domesti-bot</title>" in body
    assert "domesti-bot is running" not in body
    assert "<h1" not in body
    assert "Endpoints" not in body
    assert "/v1/execute-line" not in body


def test_root_landing_page_is_excluded_from_openapi_schema() -> None:
    client, _app = _client()
    response = client.get("/openapi.json")
    assert response.status_code == HTTPStatus.OK
    paths = response.json().get("paths", {})
    assert "/" not in paths
    assert "/favicon.ico" not in paths
    assert "/sw.js" not in paths
    assert "/health" in paths
    assert "/v1/completion-aliases" in paths


def test_root_landing_page_links_web_app_manifest() -> None:
    client, _app = _client()
    body = client.get("/").text
    assert 'rel="manifest"' in body
    assert 'href="/static/manifest.webmanifest"' in body


def test_root_landing_page_references_bundle() -> None:
    """The landing page must load /static/dist/main.js.

    The TypeScript bundle (built by ``pnpm run build`` under ``web/``)
    mounts the tile UI; tests in this module deliberately don't run the
    bundle (no headless browser), only assert the HTML contract.
    """
    client, _app = _client()
    body = client.get("/").text
    assert 'src="/static/dist/main.js"' in body


def test_service_worker_js_is_served_at_root() -> None:
    client, _app = _client()
    response = client.get("/sw.js")
    assert response.status_code == HTTPStatus.OK
    ctype = response.headers.get("content-type", "")
    assert "javascript" in ctype
    assert b"addEventListener" in response.content


def test_static_index_html_is_served_directly_at_static_mount() -> None:
    """``/static/index.html`` must be reachable through the StaticFiles mount.

    This proves the mount is wired correctly and pinpoints any future
    breakage to the mount itself rather than the ``/`` handler.
    """
    client, _app = _client()
    response = client.get("/static/index.html")
    assert response.status_code == HTTPStatus.OK
    ctype = response.headers.get("content-type", "")
    assert ctype.startswith("text/html"), ctype
    assert 'id="app"' in response.text


def test_static_manifest_webmanifest_defines_icons() -> None:
    client, _app = _client()
    response = client.get("/static/manifest.webmanifest")
    assert response.status_code == HTTPStatus.OK
    payload = json.loads(response.text)
    assert payload.get("display") == "standalone"
    assert payload.get("start_url") == "/"
    icons = payload.get("icons")
    assert isinstance(icons, list) and len(icons) >= 2
    srcs = {item.get("src") for item in icons if isinstance(item, dict)}
    assert "/static/icons/app-icon-192x192.png" in srcs
    assert "/static/icons/app-icon-512x512.png" in srcs


def test_static_missing_bundle_returns_clean_404() -> None:
    """When ``app/api/static/dist/main.js`` is missing (fresh clone, no build),
    a request must 404 cleanly without crashing the server.
    """
    client, _app = _client()
    response = client.get("/static/dist/does-not-exist.js")
    assert response.status_code == HTTPStatus.NOT_FOUND
