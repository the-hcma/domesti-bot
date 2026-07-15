"""Persist vacation-mode latch and hysteresis dwell clocks in SQLite.

Restart policy: the sticky ``armed`` bit and ``far_since`` / ``near_since``
dwell clocks survive process restart. On the next tick the live far-from-home
predicate reconciles those clocks (clears the stale side, starts the other when
needed) without wiping ``armed`` or resetting an still-valid dwell timer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from app.db.models import VacationModeState
from app.db.session import discovery_session, discovery_write

_VACATION_MODE_STATE_ID = 1


@dataclass(frozen=True)
class VacationModeStateRecord:
    armed: bool
    far_since: float | None
    near_since: float | None


def load_vacation_mode_state(path: Path) -> VacationModeStateRecord:
    """Return the persisted latch, or defaults when the row is absent."""
    with discovery_session(path) as session:
        row = session.get(VacationModeState, _VACATION_MODE_STATE_ID)
        if row is None:
            return VacationModeStateRecord(
                armed=False,
                far_since=None,
                near_since=None,
            )
        return VacationModeStateRecord(
            armed=bool(row.armed),
            far_since=row.far_since,
            near_since=row.near_since,
        )


def save_vacation_mode_state(
    path: Path,
    *,
    armed: bool,
    far_since: float | None,
    near_since: float | None,
) -> None:
    """Upsert the singleton vacation-mode latch row."""
    now = time.time()

    def _write(session: Session) -> None:
        row = session.get(VacationModeState, _VACATION_MODE_STATE_ID)
        if row is None:
            session.add(
                VacationModeState(
                    armed=1 if armed else 0,
                    far_since=far_since,
                    id=_VACATION_MODE_STATE_ID,
                    near_since=near_since,
                    updated_at=now,
                )
            )
            return
        row.armed = 1 if armed else 0
        row.far_since = far_since
        row.near_since = near_since
        row.updated_at = now

    discovery_write(path, _write)
