"""Persist automation rule fire timestamps and errors in SQLite."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AutomationRuleState
from app.db.session import discovery_session, discovery_write


@dataclass(frozen=True)
class RuleFireStateRecord:
    effective_schedule_cron: str | None
    last_error: str | None
    last_fired_at: float | None
    next_evaluate_at: float | None
    rule_id: str
    schedule_materialized_for: str | None


def list_rule_fire_states(path: Path) -> dict[str, RuleFireStateRecord]:
    """Return persisted fire state keyed by rule id."""
    with discovery_session(path) as session:
        rows = session.scalars(select(AutomationRuleState)).all()
        return {
            row.rule_id: RuleFireStateRecord(
                effective_schedule_cron=row.effective_schedule_cron,
                last_error=row.last_error,
                last_fired_at=row.last_fired_at,
                next_evaluate_at=row.next_evaluate_at,
                rule_id=row.rule_id,
                schedule_materialized_for=row.schedule_materialized_for,
            )
            for row in rows
        }


def upsert_rule_fire_state(
    path: Path,
    *,
    effective_schedule_cron: str | None = None,
    last_error: str | None,
    last_fired_at: float | None,
    next_evaluate_at: float | None = None,
    rule_id: str,
    schedule_materialized_for: str | None = None,
    update_schedule_fields: bool = False,
) -> None:
    """Upsert fire state for ``rule_id``.

    When ``update_schedule_fields`` is false, persisted schedule columns are left
    unchanged on existing rows (fire-only updates).
    """
    now = time.time()

    def _write(session: Session) -> None:
        row = session.get(AutomationRuleState, rule_id)
        if row is None:
            session.add(
                AutomationRuleState(
                    effective_schedule_cron=effective_schedule_cron,
                    last_error=last_error,
                    last_fired_at=last_fired_at,
                    next_evaluate_at=next_evaluate_at,
                    rule_id=rule_id,
                    schedule_materialized_for=schedule_materialized_for,
                    updated_at=now,
                )
            )
            return
        row.last_error = last_error
        row.last_fired_at = last_fired_at
        row.updated_at = now
        if update_schedule_fields:
            row.effective_schedule_cron = effective_schedule_cron
            row.next_evaluate_at = next_evaluate_at
            row.schedule_materialized_for = schedule_materialized_for

    discovery_write(path, _write)
