"""Persist pending rule-fire notification emails until delayed actions finish.

When ``notify_on_fire`` is set and a rule enqueues ``delay_s`` follow-ups, the
email is held here (keyed by ``rule_id`` + ``fire_at``) until every matching
row in ``rule_deferred_device_actions`` has been dispatched or cancelled.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import RulePendingFireNotification
from app.db.session import discovery_session, discovery_write
from app.rule_device_action_outcome import RuleDeviceActionOutcome

_LOGGER = logging.getLogger(__name__)
_OUTCOMES_JSON_LOG_MAX = 200


@dataclass(frozen=True)
class PendingFireNotificationRecord:
    """One deferred notify_on_fire email awaiting delayed device_actions."""

    cancelled_remaining: bool
    fire_at: float
    notification_detail: str | None
    outcomes: tuple[RuleDeviceActionOutcome, ...]
    row_id: int
    rule_id: str


def append_pending_fire_notification_outcomes(
    path: Path,
    *,
    fire_at: float,
    outcomes: tuple[RuleDeviceActionOutcome, ...],
    rule_id: str,
) -> PendingFireNotificationRecord | None:
    """Append outcomes to the pending row for ``(rule_id, fire_at)``; ``None`` if missing."""

    if not outcomes:
        return get_pending_fire_notification(path, fire_at=fire_at, rule_id=rule_id)

    def _write(session: Session) -> PendingFireNotificationRecord | None:
        row = _get_row(session, fire_at=fire_at, rule_id=rule_id)
        if row is None:
            return None
        existing = _try_outcomes_from_json(row.outcomes_json, row_id=row.id, rule_id=row.rule_id)
        if existing is None:
            return None
        row.outcomes_json = _outcomes_to_json(existing + list(outcomes))
        row.updated_at = time.time()
        session.flush()
        return _try_record_from_row(row)

    return discovery_write(path, _write)


def delete_pending_fire_notification(
    path: Path,
    *,
    fire_at: float,
    rule_id: str,
) -> None:
    """Delete the pending notification for one fire, if present."""

    def _write(session: Session) -> None:
        session.execute(
            delete(RulePendingFireNotification).where(
                RulePendingFireNotification.rule_id == rule_id,
                RulePendingFireNotification.fire_at == fire_at,
            )
        )

    discovery_write(path, _write)


def delete_pending_fire_notifications_for_rule(path: Path, rule_id: str) -> None:
    """Delete every pending notification belonging to ``rule_id``."""

    def _write(session: Session) -> None:
        session.execute(delete(RulePendingFireNotification).where(RulePendingFireNotification.rule_id == rule_id))

    discovery_write(path, _write)


def get_pending_fire_notification(
    path: Path,
    *,
    fire_at: float,
    rule_id: str,
) -> PendingFireNotificationRecord | None:
    """Return the pending notification for ``(rule_id, fire_at)``, or ``None``."""
    with discovery_session(path) as session:
        row = _get_row(session, fire_at=fire_at, rule_id=rule_id)
        return None if row is None else _try_record_from_row(row)


def insert_pending_fire_notification(
    path: Path,
    *,
    cancelled_remaining: bool = False,
    fire_at: float,
    notification_detail: str | None,
    outcomes: tuple[RuleDeviceActionOutcome, ...],
    rule_id: str,
) -> int:
    """Create a pending notification row; return its SQLite id."""
    now = time.time()

    def _write(session: Session) -> int:
        row = RulePendingFireNotification(
            cancelled_remaining=1 if cancelled_remaining else 0,
            fire_at=fire_at,
            notification_detail=notification_detail,
            outcomes_json=_outcomes_to_json(list(outcomes)),
            rule_id=rule_id,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        return row.id

    return discovery_write(path, _write)


def list_pending_fire_notifications(path: Path) -> list[PendingFireNotificationRecord]:
    """Return all pending notifications ordered by ``fire_at``.

    Rows whose ``outcomes_json`` cannot be decoded are logged and skipped so a
    single corrupt row cannot block the rest of the flush path.
    """
    with discovery_session(path) as session:
        rows = session.scalars(select(RulePendingFireNotification).order_by(RulePendingFireNotification.fire_at)).all()
        records: list[PendingFireNotificationRecord] = []
        for row in rows:
            record = _try_record_from_row(row)
            if record is not None:
                records.append(record)
        return records


def mark_pending_fire_notifications_cancelled_for_rule(path: Path, rule_id: str) -> list[PendingFireNotificationRecord]:
    """Flag pending rows for ``rule_id`` as cancelled; return the updated records."""

    def _write(session: Session) -> list[PendingFireNotificationRecord]:
        rows = list(
            session.scalars(
                select(RulePendingFireNotification).where(RulePendingFireNotification.rule_id == rule_id)
            ).all()
        )
        now = time.time()
        for row in rows:
            row.cancelled_remaining = 1
            row.updated_at = now
        session.flush()
        records: list[PendingFireNotificationRecord] = []
        for row in rows:
            record = _try_record_from_row(row)
            if record is not None:
                records.append(record)
        return records

    return discovery_write(path, _write)


def _get_row(
    session: Session,
    *,
    fire_at: float,
    rule_id: str,
) -> RulePendingFireNotification | None:
    return session.scalars(
        select(RulePendingFireNotification).where(
            RulePendingFireNotification.rule_id == rule_id,
            RulePendingFireNotification.fire_at == fire_at,
        )
    ).first()


def _outcomes_from_json(raw: str) -> list[RuleDeviceActionOutcome]:
    payload = json.loads(raw)
    if not isinstance(payload, list):
        preview = raw if len(raw) <= _OUTCOMES_JSON_LOG_MAX else f"{raw[:_OUTCOMES_JSON_LOG_MAX]}…"
        raise ValueError(
            f"Expected a JSON list of outcomes, got {type(payload).__name__}: {preview!r}",
        )
    return [RuleDeviceActionOutcome.from_json_dict(item) for item in payload]


def _outcomes_to_json(outcomes: list[RuleDeviceActionOutcome]) -> str:
    return json.dumps([outcome.to_json_dict() for outcome in outcomes])


def _record_from_row(row: RulePendingFireNotification) -> PendingFireNotificationRecord:
    return PendingFireNotificationRecord(
        cancelled_remaining=bool(row.cancelled_remaining),
        fire_at=row.fire_at,
        notification_detail=row.notification_detail,
        outcomes=tuple(_outcomes_from_json(row.outcomes_json)),
        row_id=row.id,
        rule_id=row.rule_id,
    )


def _try_outcomes_from_json(
    raw: str,
    *,
    row_id: int,
    rule_id: str,
) -> list[RuleDeviceActionOutcome] | None:
    try:
        return _outcomes_from_json(raw)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _LOGGER.error(
            "Skipping corrupt pending fire notification outcomes row_id=%s rule_id=%s: %s",
            row_id,
            rule_id,
            exc,
        )
        return None


def _try_record_from_row(row: RulePendingFireNotification) -> PendingFireNotificationRecord | None:
    try:
        return _record_from_row(row)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        _LOGGER.error(
            "Skipping corrupt pending fire notification row_id=%s rule_id=%s: %s",
            row.id,
            row.rule_id,
            exc,
        )
        return None
