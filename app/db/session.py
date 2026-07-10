"""Session helpers for the discovery database."""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from app.db.schema import bootstrap_schema
from app.db.writer import discovery_write, stop_discovery_writer


@contextlib.contextmanager
def discovery_session(path: Path) -> Iterator[Session]:
    """Open a transactional session after ensuring schema exists.

    Prefer this for read-only work. Mutating call sites should use
    :func:`discovery_write` so commits run on the path's single writer thread.
    """
    engine = bootstrap_schema(path)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


__all__ = ["discovery_session", "discovery_write", "stop_discovery_writer"]
