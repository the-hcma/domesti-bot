"""Protocol 2 pairing with My Tracks: stage, probe both directions, activate, promote (design:
``docs/RELAY_KEY_DIRECTIONS.md``).

Nothing is switched on either side until both directions are probed and My Tracks has activated the staged keys,
so a failure at any step leaves the previous pairing working. From the moment the activation is sent the outcome
on My Tracks is unknown until it answers, so the pairing is persisted as ``activating``, its staged values are
kept (a new pairing cannot replace them), and :func:`reconcile_pairing` settles it later (retry, or ask My Tracks
for the pairing's state).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.db.secrets import SecretsDecryptError, replace_app_secrets
from app.mytracks_logging import mytracks_log_host, mytracks_logger
from app.mytracks_relay_keys import (
    ROW_LEGACY_KEY,
    ROW_OUTBOUND,
    ROW_OUTBOUND_PENDING,
    ROW_STATE,
    abort_pending,
    generate_pairing_id,
    generate_relay_key,
    load_state,
    pending_outbound_key,
    promote_pending,
    set_pending_state,
    stage_pairing,
)
from app.mytracks_service import (
    MyTracksAdminSession,
    MyTracksAmbiguousError,
    MyTracksSyncError,
    probe_outbound_key,
)

_LOGGER = mytracks_logger(__name__)

# Retries of the activation request when the answer is lost, before leaving the pairing ``activating``.
ACTIVATION_ATTEMPTS = 3
ACTIVATION_BACKOFF_S = 2.0

DISCARDED_PAIRING_MESSAGE = (
    "My Tracks never activated the staged keys, so they were discarded; the previous pairing is unchanged. "
    "Pair again to switch to the new keys."
)

PairingActivation = Literal["active", "unconfirmed"]
Reconciliation = Literal["none", "promoted", "aborted", "unconfirmed"]

# One pairing or reconcile at a time per process: they read and write the same staged rows.
_PAIRING_LOCK = threading.Lock()


class PairingBusyError(MyTracksSyncError):
    """Another pairing or reconcile is already running in this process."""


class PairingRefusedError(MyTracksSyncError):
    """The pairing was refused before anything changed (for example: version 2 is required but unsupported)."""


@dataclass(frozen=True)
class PairingResult:
    """Outcome of :func:`run_pairing_v2`."""

    pairing_id: str
    activation: PairingActivation
    protocol_version: int = 2
    detail: str = ""


class _LegacyAdopted(Exception):  # noqa: N818 — control flow, not an error
    """My Tracks did not echo protocol 2: it adopted the inbound key as its single shared key."""

    def __init__(self, shared_key: str) -> None:
        super().__init__("My Tracks does not support relay protocol 2")
        self.shared_key = shared_key


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def adopt_legacy_shared_key(cache_path: Path, shared_key: str) -> None:
    """Record ``shared_key`` as the single protocol 1 key and drop every protocol 2 row, in one transaction."""
    replace_app_secrets(
        cache_path,
        {
            ROW_LEGACY_KEY: shared_key,
            ROW_STATE: None,
            ROW_OUTBOUND: None,
            ROW_OUTBOUND_PENDING: None,
        },
    )


def run_pairing_v2(
    cache_path: Path,
    *,
    base_url: str,
    domesti_base_url: str,
    user_location_test_url: str,
    user_location_update_url: str,
    username: str,
    password: str,
    require_v2: bool = False,
    sleep: Callable[[float], None] = _sleep,
) -> PairingResult:
    """Pair with protocol 2.

    Raises :class:`MyTracksSyncError` if it fails before activation (the previous pairing stays intact) and
    :class:`~app.mytracks_relay_keys.PairingInProgressError` while an earlier activation is unsettled. If My Tracks
    turns out not to support protocol 2 it has already adopted the inbound key as its one shared key, so that key is
    adopted here too (protocol 1) to keep the two sides in agreement; with ``require_v2`` that is then reported as
    a refusal.
    """
    if not _PAIRING_LOCK.acquire(blocking=False):
        raise PairingBusyError("Another pairing with My Tracks is already running; wait for it to finish")
    try:
        return _run_pairing_v2_locked(
            cache_path,
            base_url=base_url,
            domesti_base_url=domesti_base_url,
            user_location_test_url=user_location_test_url,
            user_location_update_url=user_location_update_url,
            username=username,
            password=password,
            require_v2=require_v2,
            sleep=sleep,
        )
    finally:
        _PAIRING_LOCK.release()


def _run_pairing_v2_locked(
    cache_path: Path,
    *,
    base_url: str,
    domesti_base_url: str,
    user_location_test_url: str,
    user_location_update_url: str,
    username: str,
    password: str,
    require_v2: bool,
    sleep: Callable[[float], None],
) -> PairingResult:
    inbound_key = generate_relay_key()
    outbound_key = generate_relay_key()
    pairing_id = generate_pairing_id()
    host = mytracks_log_host(base_url)

    with MyTracksAdminSession(base_url, username=username, password=password) as session:
        stage_pairing(cache_path, pairing_id=pairing_id, inbound_key=inbound_key, outbound_key=outbound_key)
        try:
            staged = session.stage_pairing(
                pairing_id=pairing_id,
                inbound_key=inbound_key,
                outbound_key=outbound_key,
                domesti_base_url=domesti_base_url,
                user_location_test_url=user_location_test_url,
                user_location_update_url=user_location_update_url,
            )
            if staged.protocol_version < 2:
                # An old My Tracks ignores the new fields and treats the inbound key as its one shared key.
                # Adopt it so the two sides still agree, and say so.
                raise _LegacyAdopted(inbound_key)
            if staged.status != "staged":
                raise MyTracksSyncError(
                    f"My Tracks answered the protocol 2 stage with status {staged.status!r}; the previous "
                    "pairing is unchanged"
                )
            if set_pending_state(cache_path, pairing_id, "probing") is None:
                raise MyTracksSyncError("The staged pairing was replaced while it was being verified; try again")
            if not session.probe_inbound(pairing_id):
                raise MyTracksSyncError(
                    "My Tracks could not deliver a test location with the new inbound key; the previous pairing "
                    "is unchanged"
                )
            if not probe_outbound_key(base_url=base_url, outbound_key=outbound_key, pairing_id=pairing_id):
                raise MyTracksSyncError(
                    "My Tracks did not accept the new outbound key; the previous pairing is unchanged"
                )
        except _LegacyAdopted as adopted:
            abort_pending(cache_path, pairing_id)
            adopt_legacy_shared_key(cache_path, adopted.shared_key)
            if require_v2:
                raise PairingRefusedError(
                    "My Tracks does not support relay protocol 2, so it kept one shared key; domesti-bot adopted "
                    "that key to stay in sync, but pairing with version 2 is required in settings"
                ) from None
            return PairingResult(
                pairing_id=pairing_id,
                activation="active",
                protocol_version=1,
                detail="My Tracks does not support relay protocol 2; paired with version 1 (one shared key).",
            )
        except BaseException:
            _discard_staged(cache_path, session, pairing_id)
            raise

        # From here on the outcome on My Tracks is unknown until it answers: persist that before sending.
        if set_pending_state(cache_path, pairing_id, "activating") is None:
            _discard_staged(cache_path, session, pairing_id)
            raise MyTracksSyncError("The staged pairing was replaced before it could be activated; try again")
        outcome = _activate_with_retries(session, cache_path, pairing_id, sleep=sleep, discard_on_rejection=True)
    _LOGGER.info("pairing %s with %s finished: %s", pairing_id[:6], host, outcome)
    if outcome == "promoted":
        return PairingResult(pairing_id=pairing_id, activation="active")
    if outcome == "aborted":
        raise MyTracksSyncError("My Tracks did not activate the new keys; the previous pairing is unchanged")
    return PairingResult(
        pairing_id=pairing_id,
        activation="unconfirmed",
        detail=(
            "My Tracks has not confirmed the activation yet. The new keys are kept and will be reconciled "
            "the next time you pair or reconcile; the previous pairing still works until then."
        ),
    )


def _discard_staged(cache_path: Path, session: MyTracksAdminSession, pairing_id: str) -> None:
    """Drop a pairing that never activated; failures here must not mask the error that got us here."""
    try:
        abort_pending(cache_path, pairing_id)
    except Exception:  # the staged values expire on their own; keep the original error
        _LOGGER.warning("could not discard the staged pairing locally")
    try:
        session.abort(pairing_id)
    except Exception:
        _LOGGER.info("could not discard the staged pairing on My Tracks; it will expire")


def _activate_with_retries(
    session: MyTracksAdminSession,
    cache_path: Path,
    pairing_id: str,
    *,
    sleep: Callable[[float], None],
    discard_on_rejection: bool = False,
) -> Reconciliation:
    """Send the activation, retrying a lost answer; settle the pairing when My Tracks answers.

    A rejection (bad session, CSRF, 4xx) is a definite "not activated"; with ``discard_on_rejection`` the staged
    pairing is dropped before the error propagates.
    """
    for attempt in range(ACTIVATION_ATTEMPTS):
        try:
            status = session.activate(pairing_id)
        except MyTracksAmbiguousError:
            if attempt + 1 < ACTIVATION_ATTEMPTS:
                sleep(ACTIVATION_BACKOFF_S * (attempt + 1))
            continue
        except MyTracksSyncError:
            if discard_on_rejection:
                _discard_staged(cache_path, session, pairing_id)
            raise
        return _settle(session, cache_path, pairing_id, status)
    # Every attempt was lost. Ask for the pairing's state once; if that is lost too, stay ``activating``.
    try:
        return _settle(session, cache_path, pairing_id, session.state(pairing_id))
    except MyTracksAmbiguousError:
        return "unconfirmed"


def _settle(session: MyTracksAdminSession, cache_path: Path, pairing_id: str, status: str) -> Reconciliation:
    """Act on My Tracks's answer for the pairing: promote when active, abort only when it never activated."""
    if status == "active":
        return _promote(cache_path, pairing_id)
    if status in ("unknown", "expired"):
        # My Tracks says it has no such pending pairing; confirm it is not active before discarding anything.
        try:
            confirmed = session.state(pairing_id)
        except MyTracksAmbiguousError:
            return "unconfirmed"
        if confirmed == "active":
            return _promote(cache_path, pairing_id)
        abort_pending(cache_path, pairing_id)
        return "aborted"
    return "unconfirmed"


def _promote(cache_path: Path, pairing_id: str) -> Reconciliation:
    if promote_pending(cache_path, pairing_id):
        return "promoted"
    # My Tracks activated this pairing but the staged values are gone here: nothing was changed locally, so keep
    # whatever pending state exists and report it as unsettled instead of claiming success.
    _LOGGER.error("My Tracks activated pairing %s but its staged values are missing locally", pairing_id[:6])
    return "unconfirmed"


def reconcile_pairing(
    cache_path: Path,
    *,
    base_url: str,
    username: str,
    password: str,
    sleep: Callable[[float], None] = _sleep,
) -> Reconciliation:
    """Settle a pairing left ``activating``: retry the activation or ask My Tracks, then promote or abort."""
    if not _PAIRING_LOCK.acquire(blocking=False):
        raise PairingBusyError("A pairing with My Tracks is already running; wait for it to finish")
    try:
        pending = load_state(cache_path).pending
        if pending is None or pending.state != "activating":
            return "none"
        with MyTracksAdminSession(base_url, username=username, password=password) as session:
            try:
                status = session.state(pending.pairing_id)
            except MyTracksAmbiguousError:
                return "unconfirmed"
            if pending_outbound_key(cache_path) is None:
                # The staged outbound key is gone (a lost Fernet key, say), so this pairing cannot be completed
                # here whatever My Tracks says. Drop it so a new pairing can be made.
                abort_pending(cache_path, pending.pairing_id)
                if status == "active":
                    raise MyTracksSyncError(
                        "My Tracks activated the new keys, but their staged copy is missing on domesti-bot. "
                        "Pair again to put both sides back in sync."
                    )
                return "aborted"
            if status == "staged":
                return _activate_with_retries(session, cache_path, pending.pairing_id, sleep=sleep)
            return _settle(session, cache_path, pending.pairing_id, status)
    finally:
        _PAIRING_LOCK.release()


def reconcile_with_stored_credentials(
    cache_path: Path,
    *,
    base_url: str,
    username: str,
    load_password: Callable[[Path], str | None],
) -> Reconciliation:
    """Settle an ``activating`` pairing using the saved My Tracks admin password, if there is one (server start)."""
    pending = load_state(cache_path).pending
    if pending is None or pending.state != "activating":
        return "none"
    try:
        password = load_password(cache_path)
    except SecretsDecryptError:
        password = None
    if not password:
        return "unconfirmed"
    try:
        return reconcile_pairing(cache_path, base_url=base_url, username=username, password=password)
    except MyTracksSyncError:
        return "unconfirmed"
