"""Tests for :mod:`app.logging_config` (formatters, filters, env wiring)."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import pytest

from app.logging_config import (
    TRACE_LEVEL,
    HealthCheckFilter,
    LocalTimeFormatter,
    LogTagFilter,
    apply_logging_from_env,
    build_dict_config,
    logtag_for_record,
)


@pytest.fixture(autouse=True)
def _restore_logging_state() -> Any:
    """Snapshot/restore root and ``app`` loggers + relevant env so tests don't bleed."""

    root = logging.getLogger()
    saved_root_handlers = list(root.handlers)
    saved_root_level = root.level
    saved_app = logging.getLogger("app")
    saved_app_handlers = list(saved_app.handlers)
    saved_app_level = saved_app.level
    saved_env = {k: os.environ.get(k) for k in ("DOMESTI_LOG_LEVEL", "LOG_FILE", "LOG_UTC", "DOMESTI_LOG_CONSOLE")}
    yield
    root.handlers = saved_root_handlers
    root.setLevel(saved_root_level)
    saved_app.handlers = saved_app_handlers
    saved_app.setLevel(saved_app_level)
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _make_record(msg: str, *, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


def test_health_filter_demotes_health_endpoint_records_to_trace() -> None:
    record = _make_record('"GET /health HTTP/1.1" 200 OK')
    assert HealthCheckFilter().filter(record) is True
    assert record.levelno == TRACE_LEVEL
    assert record.levelname == "TRACE"


def test_health_filter_leaves_unrelated_records_untouched() -> None:
    record = _make_record('"POST /v1/execute-line HTTP/1.1" 200 OK')
    assert HealthCheckFilter().filter(record) is True
    assert record.levelno == logging.INFO
    assert record.levelname == "INFO"


def test_local_time_formatter_uses_my_tracks_format(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOG_UTC", raising=False)
    record = _make_record("hello %s", level=logging.INFO)
    record.args = ("world",)
    assert LogTagFilter().filter(record) is True
    formatted = LocalTimeFormatter(
        fmt="%(asctime)s.%(msecs)03d | %(levelname)-8s | %(logtag)-12s | %(message)s",
        datefmt="%Y%m%d-%H:%M:%S",
    ).format(record)
    # Format: 20260512-15:23:45.123 | INFO     | test         | hello world
    pattern = r"^\d{8}-\d{2}:\d{2}:\d{2}\.\d{3} \| INFO\s+\| test\s+\| hello world$"
    assert re.match(pattern, formatted), f"unexpected formatted line: {formatted!r}"


def test_logtag_for_record_tags_uvicorn_shutdown_as_lifecycle() -> None:
    record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Shutting down",
        args=None,
        exc_info=None,
    )
    assert logtag_for_record(record) == "lifecycle"

    startup_record = logging.LogRecord(
        name="uvicorn",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="Uvicorn running on http://127.0.0.1:8003 (Press CTRL+C to quit)",
        args=None,
        exc_info=None,
    )
    assert logtag_for_record(startup_record) == "lifecycle"

    error_record = logging.LogRecord(
        name="uvicorn.error",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="Exception in ASGI application",
        args=None,
        exc_info=None,
    )
    assert logtag_for_record(error_record) == "error"


def test_logtag_for_record_aliases_long_module_names() -> None:
    record = logging.LogRecord(
        name="app.api.mytracks_routes",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="sync",
        args=None,
        exc_info=None,
    )
    record.module = "mytracks_routes"
    assert logtag_for_record(record) == "mytracks"

    location_record = logging.LogRecord(
        name="location",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="test",
        args=None,
        exc_info=None,
    )
    location_record.module = "location_update_ingest"
    assert logtag_for_record(location_record) == "location"


def test_local_time_formatter_honours_log_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _make_record("u")
    record.created = 0.0  # 1970-01-01T00:00:00Z

    monkeypatch.setenv("LOG_UTC", "1")
    utc_line = LocalTimeFormatter(fmt="%(asctime)s | %(message)s").format(record)
    assert utc_line.startswith("1970-01-01 00:00:00")

    monkeypatch.delenv("LOG_UTC", raising=False)
    local_line = LocalTimeFormatter(fmt="%(asctime)s | %(message)s").format(record)
    # Local should still be a valid timestamp; only assert format shape, not value.
    assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", local_line)


def test_build_dict_config_console_only() -> None:
    cfg = build_dict_config(level="DEBUG", log_file=None, console=True)
    assert cfg["root"]["level"] == "DEBUG"
    assert cfg["root"]["handlers"] == ["console"]
    assert "file" not in cfg["handlers"]
    # uvicorn loggers must be wired through our handlers.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        assert cfg["loggers"][name]["handlers"] == ["console"]
        assert cfg["loggers"][name]["propagate"] is False


def test_build_dict_config_dual_logging(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "domesti-bot.log"
    cfg = build_dict_config(level="INFO", log_file=str(log_file), console=True)
    assert cfg["root"]["handlers"] == ["console", "file"]
    assert cfg["handlers"]["file"]["class"] == "logging.handlers.RotatingFileHandler"
    assert cfg["handlers"]["file"]["filename"] == str(log_file)
    assert cfg["handlers"]["file"]["maxBytes"] == 10 * 1024 * 1024
    assert cfg["handlers"]["file"]["backupCount"] == 5
    assert log_file.parent.is_dir(), "build_dict_config must create the log directory"


def test_build_dict_config_file_only(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "domesti-bot.log"
    cfg = build_dict_config(level="WARNING", log_file=str(log_file), console=False)
    assert cfg["root"]["handlers"] == ["file"]
    assert "console" not in cfg["handlers"]


def test_apply_logging_from_env_defaults_to_console(monkeypatch: pytest.MonkeyPatch) -> None:
    for k in ("DOMESTI_LOG_LEVEL", "LOG_FILE", "LOG_UTC", "DOMESTI_LOG_CONSOLE"):
        monkeypatch.delenv(k, raising=False)
    cfg = apply_logging_from_env()
    assert cfg["root"]["level"] == "INFO"
    assert cfg["root"]["handlers"] == ["console"]


def test_apply_logging_from_env_with_file_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_LOG_LEVEL", "debug")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "out.log"))
    monkeypatch.delenv("DOMESTI_LOG_CONSOLE", raising=False)
    cfg = apply_logging_from_env()
    assert cfg["root"]["level"] == "DEBUG"
    assert cfg["root"]["handlers"] == ["file"]


def test_apply_logging_from_env_console_plus_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_LOG_LEVEL", "info")
    monkeypatch.setenv("LOG_FILE", str(tmp_path / "out.log"))
    monkeypatch.setenv("DOMESTI_LOG_CONSOLE", "1")
    cfg = apply_logging_from_env()
    assert cfg["root"]["handlers"] == ["console", "file"]


def test_trace_level_is_registered_and_below_debug() -> None:
    assert TRACE_LEVEL < logging.DEBUG
    assert logging.getLevelName(TRACE_LEVEL) == "TRACE"
    # The convenience method must be installed on Logger.
    assert callable(getattr(logging.Logger, "trace", None))
