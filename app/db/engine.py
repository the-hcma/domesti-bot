"""SQLite engine factory (one cached engine per resolved database path)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import QueuePool

from app.db.bootstrap_cache import clear_bootstrap_cache

_BUSY_TIMEOUT_MS = 30_000
_POOL_MAX_OVERFLOW = 10
_POOL_SIZE = 5
_POOL_TIMEOUT_S = 30.0


def dispose_engine(path: Path) -> None:
    """Drop a cached engine (tests that swap database files)."""
    resolved = path.expanduser().resolve()
    with _engine_lock:
        hooks = tuple(_dispose_hooks)
    for hook in hooks:
        hook(resolved)
    with _engine_lock:
        engine = _engine_by_path.pop(resolved, None)
    if engine is not None:
        engine.dispose()
    clear_bootstrap_cache(path)


def get_engine(path: Path) -> Engine:
    """Return a cached SQLAlchemy engine for ``path`` (creates parent dirs)."""
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    with _engine_lock:
        cached = _engine_by_path.get(resolved)
        if cached is not None:
            return cached
        url = f"sqlite:///{resolved}"
        engine = create_engine(
            url,
            connect_args={
                "check_same_thread": False,
                "timeout": _POOL_TIMEOUT_S,
            },
            max_overflow=_POOL_MAX_OVERFLOW,
            pool_size=_POOL_SIZE,
            pool_timeout=_POOL_TIMEOUT_S,
            poolclass=QueuePool,
        )
        event.listen(engine, "connect", _configure_sqlite_connection)
        _engine_by_path[resolved] = engine
        return engine


def register_engine_dispose_hook(hook: Callable[[Path], None]) -> None:
    """Register a callback invoked at the start of :func:`dispose_engine`."""
    with _engine_lock:
        _dispose_hooks.append(hook)


def _configure_sqlite_connection(
    dbapi_connection: object,
    _connection_record: object,
) -> None:
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    try:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    finally:
        cursor.close()


_dispose_hooks: list[Callable[[Path], None]] = []
_engine_by_path: dict[Path, Engine] = {}
_engine_lock = threading.Lock()
