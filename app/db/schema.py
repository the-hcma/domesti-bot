"""Create missing tables on the discovery database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine

from app.db.base import Base
from app.db.engine import get_engine
from app.db.legacy_migrations import apply_legacy_column_migrations


def bootstrap_schema(path: Path) -> Engine:
    """Create all ORM tables and apply legacy column migrations."""
    engine = get_engine(path)
    Base.metadata.create_all(engine)
    apply_legacy_column_migrations(engine)
    return engine


def ensure_schema_if_exists(path: Path) -> None:
    """Like :func:`bootstrap_schema` but no-op when the database file is absent."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return
    bootstrap_schema(path)
