"""Persist once-per-day gates for operator digest emails."""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import OperatorDigestState
from app.db.session import discovery_session, discovery_write
from app.device_enums import OperatorDigestId


def load_operator_digest_last_sent_at(
    path: Path,
    digest_id: OperatorDigestId,
) -> float | None:
    """Return the last successful send epoch for ``digest_id``, if any."""
    with discovery_session(path) as session:
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            return None
        return row.last_sent_at


def upsert_operator_digest_last_sent_at(
    path: Path,
    *,
    digest_id: OperatorDigestId,
    last_sent_at: float,
) -> None:
    """Record a successful digest send time for the once-per-local-day gate."""

    def _write(session: Session) -> None:
        now = time.time()
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            session.add(
                OperatorDigestState(
                    digest_id=digest_id.value,
                    last_sent_at=last_sent_at,
                    updated_at=now,
                )
            )
            return
        row.last_sent_at = last_sent_at
        row.updated_at = now

    discovery_write(path, _write)
