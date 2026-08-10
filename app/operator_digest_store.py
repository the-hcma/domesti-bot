"""Persist once-per-day gates for operator digest emails."""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.db.models import OperatorDigestState
from app.db.session import discovery_session, discovery_write
from app.device_enums import OperatorDigestId

OPERATOR_DIGEST_CLAIM_TTL_S = 15 * 60.0


def complete_operator_digest_send(
    path: Path,
    *,
    digest_id: OperatorDigestId,
    last_sent_at: float,
    local_date: str,
) -> None:
    """Record a successful digest send and clear any in-flight claim."""

    def _write(session: Session) -> None:
        now = time.time()
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            session.add(
                OperatorDigestState(
                    claim_local_date=None,
                    claimed_at=None,
                    digest_id=digest_id.value,
                    last_sent_at=last_sent_at,
                    last_sent_local_date=local_date,
                    updated_at=now,
                )
            )
            return
        row.claim_local_date = None
        row.claimed_at = None
        row.last_sent_at = last_sent_at
        row.last_sent_local_date = local_date
        row.updated_at = now

    discovery_write(path, _write)


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


def release_operator_digest_claim(
    path: Path,
    *,
    digest_id: OperatorDigestId,
) -> None:
    """Clear an in-flight claim so a failed delivery can retry the same local day."""

    def _write(session: Session) -> None:
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            return
        row.claim_local_date = None
        row.claimed_at = None
        row.updated_at = time.time()

    discovery_write(path, _write)


def try_claim_operator_digest_for_local_day(
    path: Path,
    *,
    digest_id: OperatorDigestId,
    now_epoch: float,
    timezone: ZoneInfo,
    claim_ttl_s: float = OPERATOR_DIGEST_CLAIM_TTL_S,
) -> bool:
    """Atomically reserve one digest send for the home local calendar day.

    Returns True when this caller owns the reservation. Concurrent callers and
    same-day successful sends get False. Stale claims older than ``claim_ttl_s``
    may be taken over so a crashed sender does not block the day forever.
    """
    local_date = datetime.fromtimestamp(now_epoch, tz=timezone).date().isoformat()

    def _write(session: Session) -> bool:
        now = time.time()
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            session.add(
                OperatorDigestState(
                    claim_local_date=local_date,
                    claimed_at=now_epoch,
                    digest_id=digest_id.value,
                    last_sent_at=None,
                    last_sent_local_date=None,
                    updated_at=now,
                )
            )
            return True
        if _row_already_sent_on_local_date(row, local_date=local_date, timezone=timezone):
            return False
        if _row_has_active_claim(
            row,
            now_epoch=now_epoch,
            claim_ttl_s=claim_ttl_s,
        ):
            return False
        row.claim_local_date = local_date
        row.claimed_at = now_epoch
        row.updated_at = now
        return True

    return discovery_write(path, _write)


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
                    claim_local_date=None,
                    claimed_at=None,
                    digest_id=digest_id.value,
                    last_sent_at=last_sent_at,
                    last_sent_local_date=None,
                    updated_at=now,
                )
            )
            return
        row.last_sent_at = last_sent_at
        row.updated_at = now

    discovery_write(path, _write)


def _row_already_sent_on_local_date(
    row: OperatorDigestState,
    *,
    local_date: str,
    timezone: ZoneInfo,
) -> bool:
    if row.last_sent_local_date == local_date:
        return True
    if row.last_sent_at is None:
        return False
    if row.last_sent_local_date is not None:
        return False
    sent_date = datetime.fromtimestamp(row.last_sent_at, tz=timezone).date().isoformat()
    return sent_date == local_date


def _row_has_active_claim(
    row: OperatorDigestState,
    *,
    now_epoch: float,
    claim_ttl_s: float,
) -> bool:
    if row.claimed_at is None:
        return False
    return (now_epoch - row.claimed_at) < claim_ttl_s
