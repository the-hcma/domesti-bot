"""SQLite engine factory (one cached engine per resolved database path)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.pool import StaticPool

_engine_by_path: dict[Path, Engine] = {}


def _configure_sqlite_connection(dbapi_connection: object, _connection_record: object) -> None:
    dbapi_connection.execute("PRAGMA foreign_keys = ON")  # type: ignore[attr-defined]


def dispose_engine(path: Path) -> None:
    """Drop a cached engine (tests that swap database files)."""
    resolved = path.expanduser().resolve()
    engine = _engine_by_path.pop(resolved, None)
    if engine is not None:
        engine.dispose()


def get_engine(path: Path) -> Engine:
    """Return a cached SQLAlchemy engine for ``path`` (creates parent dirs)."""
    resolved = path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    cached = _engine_by_path.get(resolved)
    if cached is not None:
        return cached
    url = f"sqlite:///{resolved}"
    engine = create_engine(
        url,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    _engine_by_path[resolved] = engine
    return engine
