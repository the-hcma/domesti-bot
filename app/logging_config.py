"""Logging configuration for domesti-bot (dict-config, formatters, handlers).

Goals (per the conventions documented in ``docs/AGENTS.md``):

* **One log record per event**. Formatter shows ``YYYYMMDD-HH:MM:SS.mmm | LEVEL    | logtag       | message``.
* **System-local timestamps** by default (set ``LOG_UTC=1`` for UTC).
* **TRACE level** below DEBUG for noisy per-request lines (e.g. health checks).
* **File logging** with rotation when ``LOG_FILE`` is set; console logging when ``DOMESTI_LOG_CONSOLE=1``.
* **Transport tags** in client-activity log messages (``[http]``, future ``[http-tls]`` / ``[ws]``).

The launcher (``scripts/domesti-bot-server``) exports the environment
variables; :func:`apply_logging_from_env` installs the dict-config inside the
Python process before uvicorn boots. Both ``uvicorn``-internal loggers and
application loggers route through the same handlers, so a single tail follows
the whole service.
"""

from __future__ import annotations

import logging
import logging.config
import os
import time
from datetime import datetime, timedelta
from datetime import timezone as _tz
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo


TRACE_LEVEL: Final[int] = 5
"""Custom level below DEBUG, used to demote noise like health-check access logs."""


def _detect_system_timezone() -> ZoneInfo | _tz:
    """Best-effort discovery of the host's real timezone (independent of ``TZ``-mangling)."""

    tz_env = os.environ.get("TZ")
    if tz_env and tz_env != "UTC":
        try:
            return ZoneInfo(tz_env)
        except (KeyError, ValueError):
            pass
    try:
        link = os.readlink("/etc/localtime")
        idx = link.find("/zoneinfo/")
        if idx != -1:
            return ZoneInfo(link[idx + len("/zoneinfo/"):])
    except OSError:
        pass
    try:
        tz_name = Path("/etc/timezone").read_text().strip()
        if tz_name:
            return ZoneInfo(tz_name)
    except (OSError, KeyError, ValueError):
        pass
    return _tz(timedelta(seconds=time.localtime().tm_gmtoff))


SYSTEM_TIMEZONE: Final = _detect_system_timezone()


def _install_trace_level() -> None:
    """Register the ``TRACE`` level once. Safe to call multiple times."""

    if logging.getLevelName(TRACE_LEVEL) == "TRACE":
        return
    logging.addLevelName(TRACE_LEVEL, "TRACE")

    def _trace(self: logging.Logger, message: str, *args: object, **kwargs: Any) -> None:
        if self.isEnabledFor(TRACE_LEVEL):
            self._log(TRACE_LEVEL, message, args, **kwargs)  # type: ignore[arg-type]

    logging.Logger.trace = _trace  # type: ignore[attr-defined]


_LOGTAG_ALIASES: Final[dict[str, str]] = {
    "location_update_ingest": "location",
    "mytracks_routes": "mytracks",
    "mytracks_service": "mytracks",
    "presence_store": "presence",
}

_install_trace_level()


def format_log_timestamp(epoch: float) -> str:
    """Return a human-readable local (or UTC) timestamp for log messages."""
    tz: ZoneInfo | _tz = _tz.utc if os.environ.get("LOG_UTC") else SYSTEM_TIMEZONE
    return datetime.fromtimestamp(epoch, tz=tz).strftime("%Y-%m-%d %H:%M:%S")


_UVICORN_LIFECYCLE_MESSAGE_MARKERS: Final[tuple[str, ...]] = (
    "Application shutdown complete",
    "Application startup complete",
    "Finished server process",
    "Shutting down",
    "Started server process",
    "Uvicorn running on",
    "Waiting for application shutdown",
    "Waiting for application startup",
)


def _is_uvicorn_lifecycle_record(record: logging.LogRecord) -> bool:
    if record.name not in ("uvicorn", "uvicorn.error"):
        return False
    msg = record.getMessage()
    return any(marker in msg for marker in _UVICORN_LIFECYCLE_MESSAGE_MARKERS)


def logtag_for_record(record: logging.LogRecord) -> str:
    """Return a fixed-width-friendly logger tag for ``%(logtag)s``."""
    if _is_uvicorn_lifecycle_record(record):
        return "lifecycle"
    leaf = record.name.rsplit(".", 1)[-1]
    if leaf in _LOGTAG_ALIASES:
        return _LOGTAG_ALIASES[leaf]
    if record.module in _LOGTAG_ALIASES:
        return _LOGTAG_ALIASES[record.module]
    if len(leaf) <= 12:
        return leaf
    return record.module[:12]


class HealthCheckFilter(logging.Filter):
    """Demote ``/health`` access lines to TRACE so they don't drown real activity at INFO."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage() if record.args else str(record.msg)
        if "/health" in msg or "/health/" in msg:
            record.levelno = TRACE_LEVEL
            record.levelname = "TRACE"
        return True


class LogTagFilter(logging.Filter):
    """Attach a short ``logtag`` field so the module column stays 12 characters wide."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.logtag = logtag_for_record(record)  # type: ignore[attr-defined]
        return True


class LocalTimeFormatter(logging.Formatter):
    """Format timestamps in :data:`SYSTEM_TIMEZONE` (or UTC when ``LOG_UTC=1`` is set).

    Uses a fixed-width column layout so ``grep`` and ``tail`` stay readable in terminals.
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        tz: ZoneInfo | _tz = _tz.utc if os.environ.get("LOG_UTC") else SYSTEM_TIMEZONE
        dt = datetime.fromtimestamp(record.created, tz=tz)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S") + ",%03d" % record.msecs


def apply_logging_from_env() -> dict[str, Any]:
    """Build the dict-config from environment variables and install it.

    Recognized variables (all set by ``scripts/domesti-bot-server``):

    * ``DOMESTI_LOG_LEVEL``   — root + ``app`` + uvicorn level (default: ``INFO``).
    * ``LOG_FILE``            — when set, enables rotating file logging at that path.
    * ``LOG_UTC``             — when set (any value), timestamps render as UTC.
    * ``DOMESTI_LOG_CONSOLE`` — when set (any value), force-enable the console handler
                                even if ``LOG_FILE`` is also set (dual logging).

    Returns the dict-config that was applied, so callers can introspect it.
    """

    level = (os.environ.get("DOMESTI_LOG_LEVEL") or "INFO").upper()
    log_file = (os.environ.get("LOG_FILE") or "").strip() or None
    console = (
        not log_file  # no file → must log to console
        or bool(os.environ.get("DOMESTI_LOG_CONSOLE"))
    )
    config = build_dict_config(level=level, log_file=log_file, console=console)
    logging.config.dictConfig(config)
    return config


def build_dict_config(
    *,
    level: str = "INFO",
    log_file: str | None = None,
    console: bool = True,
) -> dict[str, Any]:
    """Return a ``logging.config.dictConfig``-compatible dict.

    * Always uses the ``LocalTimeFormatter`` ``verbose`` format.
    * Adds the console handler when ``console`` is ``True``.
    * Adds a rotating-file handler when ``log_file`` is set (10 MB × 5 backups).
    * Routes ``uvicorn`` and ``uvicorn.access`` through our handlers so the launcher's
      single tail shows everything.
    """

    handlers: dict[str, dict[str, Any]] = {}
    active: list[str] = []

    if console:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["health_check_filter", "log_tag_filter"],
        }
        active.append("console")

    if log_file:
        Path(log_file).expanduser().parent.mkdir(parents=True, exist_ok=True)
        handlers["file"] = {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(Path(log_file).expanduser()),
            "maxBytes": 10 * 1024 * 1024,  # 10 MB
            "backupCount": 5,
            "formatter": "verbose",
            "filters": ["health_check_filter", "log_tag_filter"],
        }
        active.append("file")

    if not active:
        # Defensive fallback — should never happen because at least one of
        # console / file is forced on by ``apply_logging_from_env``.
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["health_check_filter", "log_tag_filter"],
        }
        active.append("console")

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "filters": {
            "health_check_filter": {"()": f"{__name__}.HealthCheckFilter"},
            "log_tag_filter": {"()": f"{__name__}.LogTagFilter"},
        },
        "formatters": {
            "verbose": {
                "()": f"{__name__}.LocalTimeFormatter",
                "format": (
                    "%(asctime)s.%(msecs)03d | %(levelname)-8s | "
                    "%(logtag)-12s | %(message)s"
                ),
                "datefmt": "%Y%m%d-%H:%M:%S",
            },
        },
        "handlers": handlers,
        "root": {
            "handlers": active,
            "level": level,
        },
        "loggers": {
            "app": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "uvicorn": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "uvicorn.error": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "uvicorn.access": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "location": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "mytracks": {
                "handlers": active,
                "level": level,
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "propagate": False,
            },
            "httpcore": {
                "level": "WARNING",
                "propagate": False,
            },
        },
    }
