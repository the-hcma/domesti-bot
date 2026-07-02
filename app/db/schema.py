"""Create missing tables on the discovery database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine

from app.db.base import Base
from app.db.bootstrap_cache import (
    add_bootstrapped,
    bootstrap_lock,
    clear_bootstrap_cache,
    contains_bootstrapped,
)
from app.db.engine import get_engine
from app.db.schema_sync import sync_missing_columns


def bootstrap_schema(path: Path) -> Engine:
    """Create all ORM tables and sync missing columns on legacy databases."""
    engine = get_engine(path)
    resolved = path.expanduser().resolve()
    if contains_bootstrapped(resolved):
        return engine
    with bootstrap_lock():
        if contains_bootstrapped(resolved):
            return engine
        Base.metadata.create_all(engine)
        sync_missing_columns(engine)
        add_bootstrapped(resolved)
    return engine


def ensure_schema_if_exists(path: Path) -> None:
    """Like :func:`bootstrap_schema` but no-op when the database file is absent."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return
    bootstrap_schema(path)


__all__ = ["bootstrap_schema", "clear_bootstrap_cache", "ensure_schema_if_exists"]
