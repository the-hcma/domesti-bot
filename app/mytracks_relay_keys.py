"""My Tracks relay credentials, protocol version 2 (design: ``docs/RELAY_KEY_DIRECTIONS.md``).

Two keys, both generated here: ``K_in`` is what My Tracks presents to domesti-bot (we keep only a keyed
HMAC-SHA-256 verifier of it, so a copy of the database holds nothing that authenticates) and ``K_out`` is what
domesti-bot presents to My Tracks (we keep it Fernet-encrypted because we must present it). The verifier is keyed
with a random pepper that lives in ``app_secrets`` too, i.e. encrypted under the Fernet key: someone who can write
the database but does not hold the Fernet key cannot forge a verifier.

All state lives in ``app_secrets`` rows so one transaction changes it atomically and key rotation re-encrypts it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.db.secrets import (
    SecretsDecryptError,
    load_app_secret_text,
    load_mytracks_relay_api_key_from_db,
    replace_app_secrets,
    update_app_secrets,
)

PROTOCOL_LEGACY = 1
PROTOCOL_SPLIT = 2
# A staged pairing that never reached activation is discarded after this long.
PENDING_TTL_SECONDS = 30 * 60
# The previous inbound key keeps working this long after an activation, for requests already in flight.
PREVIOUS_GRACE_SECONDS = 60
# Longest presented key compared; domesti-bot issues 43 characters.
MAX_RELAY_KEY_LENGTH = 512

ROW_PEPPER = "mytracks_relay_pepper"
ROW_STATE = "mytracks_relay_state"
ROW_OUTBOUND = "mytracks_outbound_key"
ROW_OUTBOUND_PENDING = "mytracks_outbound_key_pending"
ROW_LEGACY_KEY = "mytracks_relay_api_key"
# Every row this module owns (the legacy shared key is listed separately).
RELAY_V2_ROWS = (ROW_PEPPER, ROW_STATE, ROW_OUTBOUND, ROW_OUTBOUND_PENDING)

Reader = Callable[[str], str | None]

PendingState = Literal["staged", "probing", "activating"]


class PairingInProgressError(RuntimeError):
    """A pairing is already ``activating``: My Tracks may have activated it, so it must be settled, not replaced."""


@dataclass(frozen=True)
class PendingPairing:
    """A pairing whose keys are staged on both sides but not yet active."""

    pairing_id: str
    state: PendingState
    inbound_verifier: str
    started_at: float
    expires_at: float


@dataclass(frozen=True)
class RelayState:
    """Verifier-side state. Contains no secret: only HMAC verifiers and bookkeeping."""

    protocol_version: int = PROTOCOL_LEGACY
    active_pairing_id: str = ""
    inbound_verifier: str = ""
    previous_inbound_verifier: str = ""
    previous_expires_at: float | None = None
    pending: PendingPairing | None = None


def generate_relay_key() -> str:
    """A fresh 256-bit key (43 URL-safe characters), the same shape domesti-bot has always issued."""
    return secrets.token_urlsafe(32)


def generate_pairing_id() -> str:
    """A random pairing nonce (32 URL-safe characters)."""
    return secrets.token_urlsafe(24)


def inbound_verifier(pepper: bytes, key: str) -> str:
    """Keyed HMAC-SHA-256 (hex) of a relay key; deterministic and not reversible."""
    return hmac.new(pepper, key.encode("utf-8"), hashlib.sha256).hexdigest()


def verifier_matches(pepper: bytes, presented: str, verifier: str) -> bool:
    """Constant-time check of a presented key against a stored verifier; any text, never raises."""
    if not verifier or not presented or len(presented) > MAX_RELAY_KEY_LENGTH:
        return False
    return hmac.compare_digest(inbound_verifier(pepper, presented).encode("ascii"), verifier.encode("ascii", "replace"))


def _text_or_none(path: Path, row: str) -> str | None:
    """A row's text, or ``None`` when it is absent or cannot be decrypted (a lost or changed Fernet key)."""
    try:
        return load_app_secret_text(path, row)
    except SecretsDecryptError:
        return None


def load_pepper(path: Path) -> bytes | None:
    """The stored pepper, or ``None`` when none exists yet or it cannot be decrypted."""
    return _pepper_from_text(_text_or_none(path, ROW_PEPPER))


def _pepper_from_text(raw: str | None) -> bytes | None:
    try:
        return base64.b64decode(raw, validate=True) if raw else None
    except ValueError:
        return None


def new_pepper() -> bytes:
    return secrets.token_bytes(32)


def _pepper_row(pepper: bytes) -> str:
    return base64.b64encode(pepper).decode("ascii")


def load_state(path: Path) -> RelayState:
    """The stored state; an absent or unreadable record is the legacy (protocol 1) state."""
    return _state_from_text(_text_or_none(path, ROW_STATE))


def _state_from_text(raw: str | None) -> RelayState:
    if not raw:
        return RelayState()
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return RelayState()
        pending_raw = data.get("pending")
        pending = None
        if isinstance(pending_raw, dict) and pending_raw.get("state") in ("staged", "probing", "activating"):
            pending = PendingPairing(
                pairing_id=str(pending_raw["pairing_id"]),
                state=pending_raw["state"],
                inbound_verifier=str(pending_raw["inbound_verifier"]),
                started_at=float(pending_raw["started_at"]),
                expires_at=float(pending_raw["expires_at"]),
            )
        previous_expires = data.get("previous_expires_at")
        return RelayState(
            protocol_version=int(data.get("protocol_version", PROTOCOL_LEGACY)),
            active_pairing_id=str(data.get("active_pairing_id", "")),
            inbound_verifier=str(data.get("inbound_verifier", "")),
            previous_inbound_verifier=str(data.get("previous_inbound_verifier", "")),
            previous_expires_at=float(previous_expires) if previous_expires is not None else None,
            pending=pending,
        )
    except (KeyError, TypeError, ValueError):
        return RelayState()


def _state_row(state: RelayState) -> str:
    pending = state.pending
    return json.dumps(
        {
            "protocol_version": state.protocol_version,
            "active_pairing_id": state.active_pairing_id,
            "inbound_verifier": state.inbound_verifier,
            "previous_inbound_verifier": state.previous_inbound_verifier,
            "previous_expires_at": state.previous_expires_at,
            "pending": None
            if pending is None
            else {
                "pairing_id": pending.pairing_id,
                "state": pending.state,
                "inbound_verifier": pending.inbound_verifier,
                "started_at": pending.started_at,
                "expires_at": pending.expires_at,
            },
        },
        sort_keys=True,
    )


def pending_is_usable(pending: PendingPairing | None, *, now: float | None = None) -> bool:
    """A staged pairing is usable until it expires; an ``activating`` one never expires on a guess."""
    if pending is None:
        return False
    if pending.state == "activating":
        return True
    return pending.expires_at > (time.time() if now is None else now)


def verify_inbound(path: Path, presented: str, *, now: float | None = None) -> bool:
    """Does ``presented`` authenticate My Tracks's requests to domesti-bot under protocol 2?

    Accepts the active key, the staged key of a usable pending pairing, and the previous key until its grace
    ends. Compares every candidate (no early return) so which one matched is not observable through timing.
    """
    state = load_state(path)
    pepper = load_pepper(path)
    if pepper is None or not presented:
        return False
    moment = time.time() if now is None else now
    candidates: list[str] = [state.inbound_verifier]
    if pending_is_usable(state.pending, now=moment) and state.pending is not None:
        candidates.append(state.pending.inbound_verifier)
    if state.previous_inbound_verifier and state.previous_expires_at is not None and state.previous_expires_at > moment:
        candidates.append(state.previous_inbound_verifier)
    matched = [verifier_matches(pepper, presented, candidate) for candidate in candidates]
    return any(matched)


def uses_split_keys(path: Path) -> bool:
    """True when a protocol 2 pairing is active or being staged (so the legacy single-key check is not enough)."""
    state = load_state(path)
    return state.protocol_version >= PROTOCOL_SPLIT or state.pending is not None


def stage_pairing(path: Path, *, pairing_id: str, inbound_key: str, outbound_key: str) -> PendingPairing:
    """Stage new keys without touching the active ones, in one transaction.

    Raises :class:`PairingInProgressError` while a pairing is ``activating``: My Tracks may already have activated
    it, so its verifier and ``K_out`` must be kept until it is settled. The check and the write share one writer
    transaction, so a concurrent transition cannot slip in between them.
    """

    def compute(read: Reader) -> tuple[dict[str, str | None], PendingPairing]:
        state = _state_from_text(read(ROW_STATE))
        if state.pending is not None and state.pending.state == "activating":
            raise PairingInProgressError(
                "A pairing is waiting for My Tracks to confirm its activation; settle it first (Check activation) "
                "or reset the pairing"
            )
        updates: dict[str, str | None] = {}
        pepper = _pepper_from_text(read(ROW_PEPPER))
        if pepper is None:
            pepper = new_pepper()
            updates[ROW_PEPPER] = _pepper_row(pepper)
        now = time.time()
        pending = PendingPairing(
            pairing_id=pairing_id,
            state="staged",
            inbound_verifier=inbound_verifier(pepper, inbound_key),
            started_at=now,
            expires_at=now + PENDING_TTL_SECONDS,
        )
        updates[ROW_STATE] = _state_row(_replace(state, pending=pending))
        updates[ROW_OUTBOUND_PENDING] = outbound_key
        return updates, pending

    return update_app_secrets(path, compute)


def set_pending_state(path: Path, pairing_id: str, new_state: PendingState) -> PendingPairing | None:
    """Move the pending pairing to ``probing`` or ``activating``. ``None`` when it is not the pending one."""

    def compute(read: Reader) -> tuple[dict[str, str | None], PendingPairing | None]:
        state = _state_from_text(read(ROW_STATE))
        pending = state.pending
        if pending is None or pending.pairing_id != pairing_id:
            return {}, None
        moved = PendingPairing(
            pairing_id=pending.pairing_id,
            state=new_state,
            inbound_verifier=pending.inbound_verifier,
            started_at=pending.started_at,
            expires_at=pending.expires_at,
        )
        return {ROW_STATE: _state_row(_replace(state, pending=moved))}, moved

    return update_app_secrets(path, compute)


def abort_pending(path: Path, pairing_id: str) -> bool:
    """Discard the pending pairing; the active one is untouched. ``False`` when nothing matched."""

    def compute(read: Reader) -> tuple[dict[str, str | None], bool]:
        state = _state_from_text(read(ROW_STATE))
        pending = state.pending
        if pending is None or pending.pairing_id != pairing_id:
            return {}, False
        return {ROW_STATE: _state_row(_replace(state, pending=None)), ROW_OUTBOUND_PENDING: None}, True

    return update_app_secrets(path, compute)


def expire_stale_pending(path: Path, *, now: float | None = None) -> bool:
    """Discard a pending pairing that expired before activation began. Never touches ``activating``."""
    moment = time.time() if now is None else now

    def compute(read: Reader) -> tuple[dict[str, str | None], bool]:
        state = _state_from_text(read(ROW_STATE))
        pending = state.pending
        if pending is None or pending.state == "activating" or pending.expires_at > moment:
            return {}, False
        return {ROW_STATE: _state_row(_replace(state, pending=None)), ROW_OUTBOUND_PENDING: None}, True

    return update_app_secrets(path, compute)


def promote_pending(path: Path, pairing_id: str) -> bool:
    """Make the pending pairing active (My Tracks confirmed it). Idempotent for the active pairing id.

    The previous inbound key stays valid for ``PREVIOUS_GRACE_SECONDS``. For a protocol 1 pairing that is the
    old shared key, whose plaintext row is deleted here (only its verifier is kept, for the grace window).
    """

    def compute(read: Reader) -> tuple[dict[str, str | None], bool]:
        state = _state_from_text(read(ROW_STATE))
        if state.active_pairing_id == pairing_id and state.pending is None and state.protocol_version >= PROTOCOL_SPLIT:
            return {}, True
        pending = state.pending
        pepper = _pepper_from_text(read(ROW_PEPPER))
        pending_outbound = read(ROW_OUTBOUND_PENDING)
        if pending is None or pending.pairing_id != pairing_id or pepper is None or not pending_outbound:
            return {}, False
        now = time.time()
        if state.protocol_version >= PROTOCOL_SPLIT:
            previous = state.inbound_verifier
        else:
            legacy = read(ROW_LEGACY_KEY)
            previous = inbound_verifier(pepper, legacy) if legacy else ""
        promoted = RelayState(
            protocol_version=PROTOCOL_SPLIT,
            active_pairing_id=pairing_id,
            inbound_verifier=pending.inbound_verifier,
            previous_inbound_verifier=previous,
            previous_expires_at=now + PREVIOUS_GRACE_SECONDS if previous else None,
            pending=None,
        )
        updates: dict[str, str | None] = {
            ROW_STATE: _state_row(promoted),
            ROW_OUTBOUND: pending_outbound,
            ROW_OUTBOUND_PENDING: None,
            ROW_LEGACY_KEY: None,
        }
        return updates, True

    return update_app_secrets(path, compute)


def revoke_previous(path: Path) -> bool:
    """Stop accepting the previous inbound key right now (compromise-driven rotation)."""

    def compute(read: Reader) -> tuple[dict[str, str | None], bool]:
        state = _state_from_text(read(ROW_STATE))
        if not state.previous_inbound_verifier:
            return {}, False
        cleared = _replace(state, previous_inbound_verifier="", previous_expires_at=None)
        return {ROW_STATE: _state_row(cleared)}, True

    return update_app_secrets(path, compute)


def outbound_key(path: Path) -> str | None:
    """The key to present to My Tracks: ``K_out`` under protocol 2, else the legacy shared key."""
    if load_state(path).protocol_version >= PROTOCOL_SPLIT:
        return load_app_secret_text(path, ROW_OUTBOUND)
    return load_mytracks_relay_api_key_from_db(path)


def pending_outbound_key(path: Path) -> str | None:
    """The staged ``K_out`` (for probing My Tracks before activation); ``None`` when absent or unreadable."""
    return _text_or_none(path, ROW_OUTBOUND_PENDING)


def clear_relay_rows(path: Path) -> None:
    """Delete every relay credential row, v2 and legacy (used when the pairing is cleared)."""
    replace_app_secrets(path, {name: None for name in (*RELAY_V2_ROWS, ROW_LEGACY_KEY)})


def _replace(state: RelayState, **changes: Any) -> RelayState:
    values = {
        "protocol_version": state.protocol_version,
        "active_pairing_id": state.active_pairing_id,
        "inbound_verifier": state.inbound_verifier,
        "previous_inbound_verifier": state.previous_inbound_verifier,
        "previous_expires_at": state.previous_expires_at,
        "pending": state.pending,
    }
    values.update(changes)
    return RelayState(**values)
