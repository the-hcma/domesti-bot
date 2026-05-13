"""Tests for :class:`app.api.app._AccessLogMiddleware` level selection.

The middleware emits one ``[http]`` line per request. The level is
picked at emit time based on path + status:

* Successful responses to :data:`_QUIET_ACCESS_LOG_PATHS` → DEBUG
  (poll heartbeats; ``/v1/ui/state`` is hit every 5s by the web UI
  and would otherwise dominate INFO output);
* Successful responses to any other path → INFO;
* 4xx/5xx responses to any path (including the quiet paths) → INFO,
  so genuine failures stay visible at the default log level.

These tests use ``caplog`` against the ``app.api`` logger (the same
logger ``_AccessLogMiddleware`` uses) and assert the exact level of
the emitted record so a future regression that moves all access logs
back to INFO (or all of them to DEBUG) fails loudly.
"""

from __future__ import annotations

import argparse
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import _QUIET_ACCESS_LOG_PATHS, create_app


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def _http_records(records: list[logging.LogRecord]) -> list[logging.LogRecord]:
    """Filter ``caplog.records`` to just the ``_AccessLogMiddleware`` lines."""

    return [r for r in records if r.name == "app.api" and r.getMessage().startswith("[http]")]


def test_quiet_paths_constant_lists_ui_state() -> None:
    """Guard against an accidental rename / typo that would silently
    re-enable the INFO spam — pin the constant's contents so a future
    edit needs a deliberate change here too."""

    assert "/v1/ui/state" in _QUIET_ACCESS_LOG_PATHS


def test_successful_ui_state_poll_is_logged_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, app = _client()
    # ``GET /v1/ui/state`` returns 503 Retry-After while discovery is
    # in flight; we want the *200* branch, which means flagging
    # discovery as finished and giving the dependency a populated
    # ``device_state``. Easiest path: just plant an empty state object
    # via the ``_device_state`` route's contract — the route doesn't
    # care about manager contents for this test, only that
    # ``app.state.device_state`` is truthy and ``discovery_error`` is
    # falsy.
    from unittest.mock import MagicMock

    app.state.device_state = MagicMock(
        kasa_mgr=MagicMock(switches=()),
        sonos_mgr=None,
        tailwind_mgr=None,
        cache_path=None,
    )
    app.state.discovery_error = None

    with caplog.at_level(logging.DEBUG, logger="app.api"):
        r = client.get("/v1/ui/state")
    assert r.status_code == 200

    records = _http_records(caplog.records)
    matching = [r for r in records if "/v1/ui/state" in r.getMessage()]
    assert matching, f"expected an [http] line for /v1/ui/state, got: {records}"
    # Every emitted line for ``/v1/ui/state`` on success must be DEBUG
    # — not INFO. A future regression to INFO would re-introduce the
    # log spam the user reported.
    for rec in matching:
        assert rec.levelno == logging.DEBUG, (
            f"expected DEBUG, got {rec.levelname}: {rec.getMessage()}"
        )


def test_failed_ui_state_poll_is_still_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Discovery-in-progress returns 503 with ``Retry-After: 2``. The
    failure path must stay at INFO so problems are visible without
    cranking the level to DEBUG — only the successful-poll heartbeat
    is demoted."""

    client, _app = _client()
    # No ``app.state.device_state`` set, so the dependency 503s.
    with caplog.at_level(logging.DEBUG, logger="app.api"):
        r = client.get("/v1/ui/state")
    assert r.status_code == 503

    records = _http_records(caplog.records)
    matching = [r for r in records if "/v1/ui/state" in r.getMessage()]
    assert matching
    for rec in matching:
        assert rec.levelno == logging.INFO, (
            f"expected INFO for 503, got {rec.levelname}: {rec.getMessage()}"
        )


def test_non_quiet_path_is_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Sanity check the negative: a request to an arbitrary other
    path must keep its INFO level. Uses the OpenAPI schema route
    since it's unauthenticated and always 200."""

    client, _app = _client()
    with caplog.at_level(logging.DEBUG, logger="app.api"):
        r = client.get("/openapi.json")
    assert r.status_code == 200

    records = _http_records(caplog.records)
    matching = [r for r in records if "/openapi.json" in r.getMessage()]
    assert matching
    for rec in matching:
        assert rec.levelno == logging.INFO
