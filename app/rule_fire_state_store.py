"""Persist automation rule fire timestamps and errors in SQLite."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select

from app.db.models import AutomationRuleState
from app.db.session import discovery_session


@dataclass(frozen=True)
class RuleFireStateRecord:
    last_error: str | None
    last_fired_at: float | None
    rule_id: str


def list_rule_fire_states(path: Path) -> dict[str, RuleFireStateRecord]:
    """Return persisted fire state keyed by rule id."""
    with discovery_session(path) as session:
        rows = session.scalars(select(AutomationRuleState)).all()
        return {
            row.rule_id: RuleFireStateRecord(
                last_error=row.last_error,
                last_fired_at=row.last_fired_at,
                rule_id=row.rule_id,
            )
            for row in rows
        }


def upsert_rule_fire_state(
    path: Path,
    *,
    last_error: str | None,
    last_fired_at: float | None,
    rule_id: str,
) -> None:
    """Upsert fire state for ``rule_id``."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(AutomationRuleState, rule_id)
        if row is None:
            session.add(
                AutomationRuleState(
                    rule_id=rule_id,
                    last_error=last_error,
                    last_fired_at=last_fired_at,
                    updated_at=now,
                )
            )
            return
        row.last_error = last_error
        row.last_fired_at = last_fired_at
        row.updated_at = now
