"""Persist which manual turn-off each rule's "held off" notice was already sent for.

One row per rule holds the newest ``device_manual_off`` time it was mailed about,
so a restart inside the same hold does not repeat the notice. A newer manual
turn-off has a later time and is mailed again.
"""

from __future__ import annotations

import time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import RuleHeldOffNotice
from app.db.session import discovery_session, discovery_write


def get_held_off_notified_off_at(path: Path, rule_id: str) -> float | None:
    """Return the manual-off time ``rule_id`` was last mailed about, if any."""
    with discovery_session(path) as session:
        return session.scalar(
            select(RuleHeldOffNotice.notified_off_at).where(RuleHeldOffNotice.rule_id == rule_id),
        )


def prune_held_off_notices(path: Path, keep_rule_ids: set[str]) -> None:
    """Drop rows for rules that no longer exist so the table stays bounded by the rule set."""

    def _write(session: Session) -> None:
        session.execute(delete(RuleHeldOffNotice).where(RuleHeldOffNotice.rule_id.not_in(keep_rule_ids)))

    discovery_write(path, _write)


def record_held_off_notified(path: Path, rule_id: str, off_at: float) -> None:
    """Remember that ``rule_id`` was mailed about the manual turn-off at ``off_at``."""
    now = time.time()

    def _write(session: Session) -> None:
        row = session.get(RuleHeldOffNotice, rule_id)
        if row is None:
            session.add(RuleHeldOffNotice(rule_id=rule_id, notified_off_at=off_at, updated_at=now))
            return
        row.notified_off_at = off_at
        row.updated_at = now

    discovery_write(path, _write)
