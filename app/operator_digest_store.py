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
    claim_token: float,
    digest_id: OperatorDigestId,
    last_sent_at: float,
    local_date: str,
) -> bool:
    """Record delivery for ``local_date`` after SMTP accept.

    Always persists ``last_sent_*`` for ``local_date`` so a post-midnight accept
    cannot be retried even when ``claim_token`` no longer owns the row. Clears
    the in-flight claim only when ``claim_token`` still matches.
    """

    def _write(session: Session) -> bool:
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
            return True
        owned = row.claimed_at == claim_token
        row.last_sent_at = last_sent_at
        row.last_sent_local_date = local_date
        if owned:
            row.claim_local_date = None
            row.claimed_at = None
        row.updated_at = now
        return owned

    return discovery_write(path, _write)


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
    claim_token: float,
    digest_id: OperatorDigestId,
) -> bool:
    """Release ``claim_token`` after a failed delivery so the same local day can retry."""

    def _write(session: Session) -> bool:
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None or row.claimed_at != claim_token:
            return False
        now = time.time()
        row.claim_local_date = None
        row.claimed_at = None
        # Claim reserves the day optimistically; only the owning claim may unreserve.
        row.last_sent_at = None
        row.last_sent_local_date = None
        row.updated_at = now
        return True

    return discovery_write(path, _write)


def try_claim_operator_digest_for_local_day(
    path: Path,
    *,
    digest_id: OperatorDigestId,
    now_epoch: float,
    timezone: ZoneInfo,
    claim_ttl_s: float = OPERATOR_DIGEST_CLAIM_TTL_S,
) -> float | None:
    """Atomically reserve one digest send for the home local calendar day.

    Returns a claim token (``claimed_at``) when this caller owns the reservation,
    else None. The claim optimistically records ``last_sent_*`` for today so a
    crash after SMTP acceptance cannot be retried the same day. Failed delivery
    must :func:`release_operator_digest_claim` with the same token. Active claims
    block takeover until ``claim_ttl_s`` elapses; a same-day reservation without
    release is not taken over (missed day over double-send).
    """
    local_date = datetime.fromtimestamp(now_epoch, tz=timezone).date().isoformat()

    def _write(session: Session) -> float | None:
        now = time.time()
        row = session.get(OperatorDigestState, digest_id.value)
        if row is None:
            session.add(
                OperatorDigestState(
                    claim_local_date=local_date,
                    claimed_at=now_epoch,
                    digest_id=digest_id.value,
                    last_sent_at=now_epoch,
                    last_sent_local_date=local_date,
                    updated_at=now,
                )
            )
            return now_epoch
        if _row_has_active_claim(
            row,
            now_epoch=now_epoch,
            claim_ttl_s=claim_ttl_s,
        ):
            return None
        if _row_already_sent_on_local_date(row, local_date=local_date, timezone=timezone):
            return None
        row.claim_local_date = local_date
        row.claimed_at = now_epoch
        row.last_sent_at = now_epoch
        row.last_sent_local_date = local_date
        row.updated_at = now
        return now_epoch

    return discovery_write(path, _write)


def upsert_operator_digest_last_sent_at(
    path: Path,
    *,
    digest_id: OperatorDigestId,
    last_sent_at: float,
    local_date: str,
) -> bool:
    """Record a successful digest send for the once-per-local-day gate.

    Writes ``last_sent_at`` and ``last_sent_local_date`` together. Returns False
    when an in-flight claim owns the row — production completion must use
    :func:`complete_operator_digest_send` instead.
    """

    def _write(session: Session) -> bool:
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
            return True
        if row.claimed_at is not None:
            return False
        row.last_sent_at = last_sent_at
        row.last_sent_local_date = local_date
        row.updated_at = now
        return True

    return discovery_write(path, _write)


def _row_already_sent_on_local_date(
    row: OperatorDigestState,
    *,
    local_date: str,
    timezone: ZoneInfo,
) -> bool:
    if row.last_sent_local_date == local_date:
        return True
    # Optimistic / in-flight claims always set last_sent_local_date; do not treat a
    # bare last_sent_at as finalized while a claim is still open.
    if row.claimed_at is not None:
        return False
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
