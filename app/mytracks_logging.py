"""Shared logging helpers for my-tracks integration."""

from __future__ import annotations

import logging
from urllib.parse import urlparse

MYTRACKS_LOGGER_NAME = "mytracks"


def mytracks_logger(module: str) -> logging.Logger:
    """Return the short ``mytracks`` logger (keeps the module column narrow)."""
    _ = module
    return logging.getLogger(MYTRACKS_LOGGER_NAME)


def mytracks_log_host(url: str) -> str:
    """Return a compact host label for log messages (no scheme/path noise)."""
    trimmed = url.strip()
    if trimmed == "":
        return "(unset)"
    parsed = urlparse(trimmed if "://" in trimmed else f"https://{trimmed}")
    host = parsed.netloc or parsed.path.split("/")[0]
    return host if host != "" else trimmed
