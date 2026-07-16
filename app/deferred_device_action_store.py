"""Persist pending delayed rule device actions in SQLite.

Delayed ``device_actions`` (``delay_s`` > 0) are enqueued at fire time and
dispatched when their ``due_at`` elapses. Persisting them means a follow-up step
(e.g. the HDHomeRun ``turn_off`` -> ``turn_on`` power cycle) still runs after a
process restart that happens during the delay window. Rows are removed once the
action is dispatched or when the owning rule is disabled/removed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.api.schemas import RuleDeviceActionOut
from app.db.models import RuleDeferredDeviceAction
from app.db.session import discovery_session, discovery_write


@dataclass(frozen=True)
class DeferredDeviceActionRecord:
    """One persisted delayed device action, keyed by its SQLite row id."""

    action: RuleDeviceActionOut
    due_at: float
    fire_at: float
    row_id: int
    rule_id: str


def delete_deferred_device_actions(path: Path, row_ids: list[int]) -> None:
    """Delete persisted delayed actions by SQLite row id."""
    if not row_ids:
        return

    def _write(session: Session) -> None:
        session.execute(delete(RuleDeferredDeviceAction).where(RuleDeferredDeviceAction.id.in_(row_ids)))

    discovery_write(path, _write)


def delete_deferred_device_actions_for_rule(path: Path, rule_id: str) -> None:
    """Delete all persisted delayed actions belonging to ``rule_id``."""

    def _write(session: Session) -> None:
        session.execute(delete(RuleDeferredDeviceAction).where(RuleDeferredDeviceAction.rule_id == rule_id))

    discovery_write(path, _write)


def insert_deferred_device_action(
    path: Path,
    *,
    action: RuleDeviceActionOut,
    due_at: float,
    fire_at: float,
    rule_id: str,
) -> int:
    """Persist one delayed action and return its new SQLite row id."""
    now = time.time()

    def _write(session: Session) -> int:
        row = RuleDeferredDeviceAction(
            action_json=action.model_dump_json(),
            due_at=due_at,
            fire_at=fire_at,
            rule_id=rule_id,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row.id

    return discovery_write(path, _write)


def list_deferred_device_actions(path: Path) -> list[DeferredDeviceActionRecord]:
    """Return persisted delayed actions ordered by ``due_at`` (soonest first)."""
    with discovery_session(path) as session:
        rows = session.scalars(select(RuleDeferredDeviceAction).order_by(RuleDeferredDeviceAction.due_at)).all()
        return [
            DeferredDeviceActionRecord(
                action=RuleDeviceActionOut.model_validate_json(row.action_json),
                due_at=row.due_at,
                fire_at=row.fire_at,
                row_id=row.id,
                rule_id=row.rule_id,
            )
            for row in rows
        ]
