"""Tests for :class:`app.api.app._AccessLogMiddleware` level selection.

The middleware emits one ``[http]`` line per request. The level is
picked at emit time based on path + status:

* Successful responses to :data:`_QUIET_ACCESS_LOG_PATHS` → TRACE
  (poll heartbeats; ``/v1/ui/state`` is hit every 5s by the web UI);
* Sub-500 responses on the same quiet paths (including discovery 503) → TRACE;
* Other successful responses → DEBUG (routine client traffic stays below INFO);
* 4xx/5xx responses on non-quiet API paths → INFO;
* ``/static/…`` responses (including 404 missing icons) → DEBUG.

These tests use ``caplog`` against the ``app.api`` logger (the same
logger ``_AccessLogMiddleware`` uses) and assert the exact level of
the emitted record so a future regression that moves all access logs
back to INFO (or all of them to DEBUG) fails loudly.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import _QUIET_ACCESS_LOG_PATHS, create_app
from app.server_runtime import runtime
from app.logging_config import TRACE_LEVEL


class _ListHandler(logging.Handler):
    def __init__(self, records: list[logging.LogRecord]) -> None:
        super().__init__(level=TRACE_LEVEL)
        self._records = records

    def emit(self, record: logging.LogRecord) -> None:
        self._records.append(record)


@pytest.fixture
def api_http_log_records() -> Any:
    """Capture ``app.api`` access-log lines even when dict-config disables propagation."""

    records: list[logging.LogRecord] = []
    handler = _ListHandler(records)
    logger = logging.getLogger("app.api")
    old_handlers = list(logger.handlers)
    old_level = logger.level
    old_propagate = logger.propagate
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(TRACE_LEVEL)
    logger.propagate = False
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.handlers = old_handlers
        logger.setLevel(old_level)
        logger.propagate = old_propagate


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


def test_successful_ui_state_poll_is_logged_at_trace(
    api_http_log_records: list[logging.LogRecord],
) -> None:
    client, app = _client()
    from unittest.mock import MagicMock

    runtime.device_state = MagicMock(
        kasa_mgr=MagicMock(switches=()),
        sonos_mgr=None,
        tailwind_mgr=None,
        cache_path=None,
    )
    runtime.discovery_error = None

    r = client.get("/v1/ui/state")
    assert r.status_code == 200

    records = _http_records(api_http_log_records)
    matching = [r for r in records if "/v1/ui/state" in r.getMessage()]
    assert matching, f"expected an [http] line for /v1/ui/state, got: {records}"
    for rec in matching:
        assert rec.levelno == TRACE_LEVEL, (
            f"expected TRACE, got {rec.levelname}: {rec.getMessage()}"
        )


def test_discovery_in_progress_ui_state_503_is_logged_at_trace(
    api_http_log_records: list[logging.LogRecord],
) -> None:
    """Discovery-in-progress returns 503 with ``Retry-After: 2``. The
    UI bootstrap poll hammers this endpoint every 2s — demote to TRACE
    so INFO stays readable while backends report progress."""

    client, _app = _client()
    r = client.get("/v1/ui/state")
    assert r.status_code == 503

    records = _http_records(api_http_log_records)
    matching = [r for r in records if "/v1/ui/state" in r.getMessage()]
    assert matching
    for rec in matching:
        assert rec.levelno == TRACE_LEVEL, (
            f"expected TRACE for discovery 503, got {rec.levelname}: {rec.getMessage()}"
        )


def test_non_quiet_path_is_logged_at_debug(
    api_http_log_records: list[logging.LogRecord],
) -> None:
    """Routine successful client traffic stays below INFO at the default level."""

    client, _app = _client()
    r = client.get("/openapi.json")
    assert r.status_code == 200

    records = _http_records(api_http_log_records)
    matching = [r for r in records if "/openapi.json" in r.getMessage()]
    assert matching
    for rec in matching:
        assert rec.levelno == logging.DEBUG, (
            f"expected DEBUG, got {rec.levelname}: {rec.getMessage()}"
        )


def test_static_asset_404_is_logged_at_debug(
    api_http_log_records: list[logging.LogRecord],
) -> None:
    """Missing static icons are routine browser noise — stay below INFO."""

    client, _app = _client()
    r = client.get("/static/icons/compact/tv.svg")
    assert r.status_code == 404

    records = _http_records(api_http_log_records)
    matching = [r for r in records if "/static/icons/compact/tv.svg" in r.getMessage()]
    assert matching
    for rec in matching:
        assert rec.levelno == logging.DEBUG, (
            f"expected DEBUG for static 404, got {rec.levelname}: {rec.getMessage()}"
        )


def test_successful_http_is_not_logged_at_info(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, _app = _client()
    with caplog.at_level(logging.INFO, logger="app.api"):
        r = client.get("/openapi.json")
    assert r.status_code == 200

    records = _http_records(caplog.records)
    matching = [r for r in records if "/openapi.json" in r.getMessage()]
    assert matching == []
