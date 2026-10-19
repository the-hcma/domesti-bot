"""Persist the "keep checking" answer to a device presence notice (domesti-bot#874).

When a device stops responding the operator chooses to forget it or to keep checking for it
(the outage may be transient). Only "keep checking" needs storing: without it, a plug that is
still missing would be announced again after every restart. ``forget`` removes the device
instead, and an unanswered notice is simply raised again.
"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import DevicePresenceKeepChecking
from app.db.session import discovery_session, discovery_write


def clear_keep_checking(path: Path | None, family_id: str, device_id: str) -> None:
    """Forget the "keep checking" answer for ``device_id`` (it answered again, or was forgotten)."""
    if path is None:
        return

    def _write(session: Session) -> None:
        session.execute(
            delete(DevicePresenceKeepChecking).where(
                DevicePresenceKeepChecking.family_id == family_id,
                DevicePresenceKeepChecking.device_id == device_id,
            )
        )

    discovery_write(path, _write)


def list_keep_checking(path: Path | None) -> dict[tuple[str, str], float]:
    """Return ``(family_id, device_id) -> epoch`` of each stored "keep checking" answer."""
    if path is None:
        return {}
    with discovery_session(path) as session:
        rows = session.scalars(select(DevicePresenceKeepChecking)).all()
        return {(row.family_id, row.device_id): row.decided_at for row in rows}


def record_keep_checking(
    path: Path | None,
    family_id: str,
    device_id: str,
    *,
    now: float | None = None,
) -> None:
    """Store that the operator chose to keep checking for ``device_id``; answering again restarts the clock."""
    if path is None:
        return
    at = time.time() if now is None else now

    def _write(session: Session) -> None:
        row = session.get(DevicePresenceKeepChecking, (family_id, device_id))
        if row is None:
            session.add(
                DevicePresenceKeepChecking(
                    decided_at=at,
                    device_id=device_id,
                    family_id=family_id,
                    updated_at=at,
                )
            )
            return
        row.decided_at = at
        row.updated_at = at

    discovery_write(path, _write)
