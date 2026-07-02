"""Bookkeeping for one-time schema bootstrap per database path."""

from __future__ import annotations

import threading
from pathlib import Path


def add_bootstrapped(path: Path) -> None:
    _bootstrapped_paths.add(path.expanduser().resolve())


def bootstrap_lock() -> threading.Lock:
    return _bootstrap_lock


def clear_bootstrap_cache(path: Path | None = None) -> None:
    """Drop bootstrap bookkeeping (tests that swap or remove database files)."""
    with _bootstrap_lock:
        if path is None:
            _bootstrapped_paths.clear()
            return
        _bootstrapped_paths.discard(path.expanduser().resolve())


def contains_bootstrapped(path: Path) -> bool:
    return path.expanduser().resolve() in _bootstrapped_paths


_bootstrap_lock = threading.Lock()
_bootstrapped_paths: set[Path] = set()
