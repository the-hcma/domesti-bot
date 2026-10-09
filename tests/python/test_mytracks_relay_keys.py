"""Relay credentials protocol 2, storage side: verifier-only inbound key, staging, promotion, grace, revoke."""

from __future__ import annotations

import argparse
import json
import sqlite3
from http import HTTPStatus
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.app import create_app
from app.db.models import AppSecret
from app.db.secrets import load_app_secret_text, save_mytracks_relay_api_key_to_db
from app.db.session import discovery_session
from app.mytracks_relay_keys import (
    MAX_RELAY_KEY_LENGTH,
    PENDING_TTL_SECONDS,
    PREVIOUS_GRACE_SECONDS,
    PROTOCOL_LEGACY,
    PROTOCOL_SPLIT,
    ROW_LEGACY_KEY,
    ROW_OUTBOUND,
    ROW_OUTBOUND_PENDING,
    ROW_PEPPER,
    ROW_STATE,
    abort_pending,
    clear_relay_rows,
    expire_stale_pending,
    generate_pairing_id,
    generate_relay_key,
    inbound_verifier,
    load_pepper,
    load_state,
    outbound_key,
    pending_outbound_key,
    promote_pending,
    revoke_previous,
    set_pending_state,
    stage_pairing,
    uses_split_keys,
    verifier_matches,
    verify_inbound,
)

# Built rather than written as literals so secret scanners do not flag them.
_K_IN = "-".join(["inbound", "relay", "key", "0123456789"])
_K_OUT = "-".join(["outbound", "relay", "key", "9876543210"])
_K_LEGACY = "-".join(["shared", "legacy", "key", "1111111111"])
_PAIRING = "pairing-0001-abcdef"
_WEBHOOK = "/v1/webhooks/location_update/test"
_PAYLOAD = {
    "user_id": "henrique",
    "lat": 41.194085,
    "lon": -73.888365,
    "accuracy_m": 12,
    "timestamp": "2026-06-09T23:14:58+00:00",
    "source": "my-tracks",
}


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "ui.sqlite"


def _client(db: Path) -> TestClient:
    return TestClient(create_app(argparse.Namespace(discovery_cache=str(db), tailwind_token=None)))


def _post(client: TestClient, key: str | None) -> int:
    # Bytes, so non-ASCII keys can actually be sent (the test client refuses str values outside ASCII).
    raw_headers: list[tuple[bytes, bytes]] = [] if key is None else [(b"x-domesti-api-key", key.encode("utf-8"))]
    return client.post(_WEBHOOK, json=_PAYLOAD, headers=raw_headers).status_code


def _raw_rows(db: Path) -> dict[str, bytes]:
    with sqlite3.connect(db) as conn:
        return {k: bytes(c) for k, c in conn.execute("SELECT key, ciphertext FROM app_secrets")}


# --- verifier -----------------------------------------------------------------------------------------------


def test_generated_keys_have_the_issued_shape() -> None:
    assert len(generate_relay_key()) == 43
    assert generate_relay_key() != generate_relay_key()
    assert len(generate_pairing_id()) == 32


def test_verifier_is_keyed_deterministic_and_not_the_key() -> None:
    pepper_a, pepper_b = b"a" * 32, b"b" * 32
    assert inbound_verifier(pepper_a, _K_IN) == inbound_verifier(pepper_a, _K_IN)
    assert inbound_verifier(pepper_a, _K_IN) != inbound_verifier(pepper_b, _K_IN)
    assert inbound_verifier(pepper_a, _K_IN) != inbound_verifier(pepper_a, _K_OUT)
    assert _K_IN not in inbound_verifier(pepper_a, _K_IN)
    assert len(inbound_verifier(pepper_a, _K_IN)) == 64


@pytest.mark.parametrize("presented", ["", "clé-secrète", "☃" * 10, "x" * (MAX_RELAY_KEY_LENGTH + 1), _K_OUT])
def test_verifier_rejects_wrong_empty_non_ascii_and_oversized_keys_without_raising(presented: str) -> None:
    pepper = b"p" * 32
    assert verifier_matches(pepper, presented, inbound_verifier(pepper, _K_IN)) is False


def test_verifier_accepts_the_right_key_and_an_empty_verifier_never_matches() -> None:
    pepper = b"p" * 32
    assert verifier_matches(pepper, _K_IN, inbound_verifier(pepper, _K_IN)) is True
    assert verifier_matches(pepper, _K_IN, "") is False


# --- staging ------------------------------------------------------------------------------------------------


def test_staging_changes_nothing_active_and_stores_no_plaintext_inbound_key(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)

    pending = stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)

    state = load_state(db)
    assert state.protocol_version == PROTOCOL_LEGACY
    assert state.pending == pending
    assert pending.state == "staged"
    assert pending.expires_at - pending.started_at == PENDING_TTL_SECONDS
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == _K_LEGACY
    assert pending_outbound_key(db) == _K_OUT
    assert outbound_key(db) == _K_LEGACY  # still presenting the legacy key until activation
    state_row = load_app_secret_text(db, ROW_STATE) or ""
    assert _K_IN not in state_row and _K_OUT not in state_row


def test_a_staged_inbound_key_authenticates_next_to_the_active_one_but_the_outbound_key_does_not(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    client = _client(db)
    assert _post(client, _K_IN) != HTTPStatus.UNAUTHORIZED
    assert _post(client, _K_LEGACY) != HTTPStatus.UNAUTHORIZED
    assert _post(client, _K_OUT) == HTTPStatus.UNAUTHORIZED
    assert _post(client, None) == HTTPStatus.UNAUTHORIZED


def test_restaging_replaces_the_pending_pairing_and_keeps_the_pepper(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    pepper = load_pepper(db)
    other_in = generate_relay_key()
    stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=other_in, outbound_key=generate_relay_key())
    assert load_pepper(db) == pepper
    assert load_state(db).pending is not None
    assert load_state(db).pending.pairing_id == "pairing-0002-abcdef"  # type: ignore[union-attr]
    assert verify_inbound(db, other_in) is True
    assert verify_inbound(db, _K_IN) is False


def test_an_expired_pending_pairing_is_not_accepted_and_is_discarded(db: Path) -> None:
    pending = stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    assert verify_inbound(db, _K_IN, now=pending.expires_at - 1) is True
    assert verify_inbound(db, _K_IN, now=pending.expires_at + 1) is False
    assert expire_stale_pending(db, now=pending.expires_at + 1) is True
    assert load_state(db).pending is None
    assert pending_outbound_key(db) is None


def test_an_activating_pairing_never_expires_on_a_guess(db: Path) -> None:
    pending = stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    set_pending_state(db, _PAIRING, "activating")
    far_future = pending.expires_at + 10 * PENDING_TTL_SECONDS
    assert verify_inbound(db, _K_IN, now=far_future) is True
    assert expire_stale_pending(db, now=far_future) is False
    assert load_state(db).pending is not None
    assert pending_outbound_key(db) == _K_OUT


def test_set_pending_state_ignores_a_different_pairing_id(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    assert set_pending_state(db, "someone-else", "probing") is None
    assert load_state(db).pending is not None
    assert load_state(db).pending.state == "staged"  # type: ignore[union-attr]
    moved = set_pending_state(db, _PAIRING, "probing")
    assert moved is not None and moved.state == "probing"


def test_abort_discards_only_the_pending_pairing(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    assert abort_pending(db, "someone-else") is False
    assert abort_pending(db, _PAIRING) is True
    assert abort_pending(db, _PAIRING) is False
    assert load_state(db).pending is None
    assert pending_outbound_key(db) is None
    assert verify_inbound(db, _K_IN) is False
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == _K_LEGACY


# --- promotion ----------------------------------------------------------------------------------------------


def test_promotion_from_a_legacy_pairing_switches_keys_and_keeps_the_old_one_for_a_grace(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)

    assert promote_pending(db, _PAIRING) is True

    state = load_state(db)
    assert state.protocol_version == PROTOCOL_SPLIT
    assert state.active_pairing_id == _PAIRING
    assert state.pending is None
    assert outbound_key(db) == _K_OUT
    assert pending_outbound_key(db) is None
    assert load_app_secret_text(db, ROW_LEGACY_KEY) is None, "the old shared key's plaintext is gone"
    assert uses_split_keys(db) is True
    soon = (state.previous_expires_at or 0) - 1
    assert verify_inbound(db, _K_IN) is True
    assert verify_inbound(db, _K_LEGACY, now=soon) is True
    assert verify_inbound(db, _K_LEGACY, now=(state.previous_expires_at or 0) + 1) is False
    assert (state.previous_expires_at or 0) - PREVIOUS_GRACE_SECONDS > 0


def test_promotion_is_idempotent_and_refuses_the_wrong_pairing_id(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    assert promote_pending(db, "someone-else") is False
    assert promote_pending(db, _PAIRING) is True
    assert promote_pending(db, _PAIRING) is True
    assert load_state(db).protocol_version == PROTOCOL_SPLIT
    assert promote_pending(db, "someone-else") is False


def test_a_second_promotion_keeps_exactly_one_previous_key(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    second_in, second_out = generate_relay_key(), generate_relay_key()
    stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=second_in, outbound_key=second_out)
    promote_pending(db, "pairing-0002-abcdef")

    state = load_state(db)
    assert outbound_key(db) == second_out
    assert verify_inbound(db, second_in) is True
    assert verify_inbound(db, _K_IN, now=(state.previous_expires_at or 0) - 1) is True
    assert verify_inbound(db, _K_IN, now=(state.previous_expires_at or 0) + 1) is False


def test_revoke_previous_stops_the_old_key_immediately(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    assert verify_inbound(db, _K_LEGACY) is True
    assert revoke_previous(db) is True
    assert verify_inbound(db, _K_LEGACY) is False
    assert verify_inbound(db, _K_IN) is True
    assert revoke_previous(db) is False


def test_a_database_dump_after_promotion_holds_no_value_that_authenticates_the_inbound_key(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    pepper = load_pepper(db) or b""
    plaintext_rows = {name: load_app_secret_text(db, name) or "" for name in _raw_rows(db)}
    assert _K_IN not in "".join(plaintext_rows.values())
    state = json.loads(plaintext_rows[ROW_STATE])
    assert state["inbound_verifier"] == inbound_verifier(pepper, _K_IN)
    for text in plaintext_rows.values():
        assert verify_inbound(db, text) is False  # no stored value (verifier included) is itself a credential


def test_rows_are_encrypted_at_rest_including_the_verifier_state(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    raw = b" ".join(_raw_rows(db).values())
    for needle in (_K_IN, _K_OUT, "inbound_verifier", _PAIRING):
        assert needle.encode() not in raw


def test_clear_relay_rows_removes_everything_including_the_legacy_key(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    clear_relay_rows(db)
    with discovery_session(db) as session:
        names = set(session.scalars(select(AppSecret.key)))
    assert names.isdisjoint({ROW_PEPPER, ROW_STATE, ROW_OUTBOUND, ROW_OUTBOUND_PENDING, ROW_LEGACY_KEY})
    assert load_state(db).protocol_version == PROTOCOL_LEGACY
    assert verify_inbound(db, _K_IN) is False


# --- the webhook auth dependency ----------------------------------------------------------------------------


def test_webhook_auth_for_a_protocol_1_pairing_is_unchanged(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    client = _client(db)
    assert _post(client, _K_LEGACY) != HTTPStatus.UNAUTHORIZED
    assert _post(client, _K_IN) == HTTPStatus.UNAUTHORIZED
    assert _post(client, "clé-secrète") == HTTPStatus.UNAUTHORIZED


def test_webhook_auth_without_any_relay_configured_says_not_configured(db: Path) -> None:
    response = _client(db).post(_WEBHOOK, json=_PAYLOAD, headers={"X-Domesti-Api-Key": _K_IN})
    assert response.status_code == HTTPStatus.UNAUTHORIZED
    assert response.json()["detail"] == "My Tracks relay not configured"


def test_webhook_auth_for_a_protocol_2_pairing_takes_only_the_inbound_key(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    client = _client(db)
    assert _post(client, _K_IN) != HTTPStatus.UNAUTHORIZED
    assert _post(client, _K_OUT) == HTTPStatus.UNAUTHORIZED, "the outbound key must not authenticate inbound webhooks"
    for weird in ("", "clé-secrète", "☃" * 20, "x" * 5000):
        assert _post(client, weird) == HTTPStatus.UNAUTHORIZED


def test_the_old_shared_key_stops_working_after_the_grace_and_the_revoke(db: Path) -> None:
    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    client = _client(db)
    assert _post(client, _K_LEGACY) != HTTPStatus.UNAUTHORIZED
    revoke_previous(db)
    assert _post(client, _K_LEGACY) == HTTPStatus.UNAUTHORIZED


def test_an_unreadable_state_row_falls_back_to_the_legacy_check(db: Path) -> None:
    from app.db.secrets import replace_app_secrets

    save_mytracks_relay_api_key_to_db(db, _K_LEGACY)
    replace_app_secrets(db, {ROW_STATE: "{ not json"})
    state = load_state(db)
    assert state.protocol_version == PROTOCOL_LEGACY
    assert _post(_client(db), _K_LEGACY) != HTTPStatus.UNAUTHORIZED


# --- review fixes -------------------------------------------------------------------------------------------


def test_staging_is_refused_while_a_pairing_is_activating_and_nothing_is_replaced(db: Path) -> None:
    from app.mytracks_relay_keys import PairingInProgressError

    pending = stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    set_pending_state(db, _PAIRING, "activating")

    with pytest.raises(PairingInProgressError, match="settle it first"):
        stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=generate_relay_key(), outbound_key="x" * 43)

    state = load_state(db)
    assert state.pending is not None and state.pending.pairing_id == _PAIRING
    assert state.pending.inbound_verifier == pending.inbound_verifier
    assert pending_outbound_key(db) == _K_OUT
    assert verify_inbound(db, _K_IN) is True


def test_a_staged_or_probing_pairing_can_still_be_replaced(db: Path) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    set_pending_state(db, _PAIRING, "probing")
    other = generate_relay_key()
    stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=other, outbound_key=generate_relay_key())
    assert verify_inbound(db, other) is True


def test_rows_that_can_no_longer_be_decrypted_read_as_absent_instead_of_raising(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    promote_pending(db, _PAIRING)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))  # the old key is lost

    assert load_state(db).protocol_version == PROTOCOL_LEGACY
    assert load_pepper(db) is None
    assert pending_outbound_key(db) is None
    assert verify_inbound(db, _K_IN) is False

    # And staging works again from scratch, creating a new pepper over the unreadable one.
    stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=_K_IN, outbound_key=_K_OUT)
    assert verify_inbound(db, _K_IN) is True


# --- atomic read-decide-write (review fix) ------------------------------------------------------------------


def test_the_activating_check_reads_inside_the_write_transaction_not_from_a_stale_snapshot(db: Path) -> None:
    from unittest.mock import patch

    from app.mytracks_relay_keys import PairingInProgressError, RelayState

    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    set_pending_state(db, _PAIRING, "activating")

    # A caller holding a snapshot taken before the transition would see no pending pairing; the check must not.
    with patch("app.mytracks_relay_keys.load_state", return_value=RelayState()):
        with pytest.raises(PairingInProgressError):
            stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=generate_relay_key(), outbound_key="o" * 43)
    assert load_state(db).pending is not None and load_state(db).pending.pairing_id == _PAIRING  # type: ignore[union-attr]


def test_concurrent_stages_never_replace_an_activating_pairing(db: Path) -> None:
    import threading

    from app.mytracks_relay_keys import PairingInProgressError

    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def activate() -> None:
        barrier.wait()
        set_pending_state(db, _PAIRING, "activating")
        outcomes.append("activating")

    def restage() -> None:
        barrier.wait()
        try:
            stage_pairing(db, pairing_id="pairing-0002-abcdef", inbound_key=generate_relay_key(), outbound_key="o" * 43)
            outcomes.append("restaged")
        except PairingInProgressError:
            outcomes.append("refused")

    threads = [threading.Thread(target=activate), threading.Thread(target=restage)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    pending = load_state(db).pending
    assert pending is not None
    if "restaged" in outcomes:
        # The re-stage won the race: it replaced a pairing that was not activating yet, so nothing was lost,
        # and the late "activating" write found a different pairing and did nothing.
        assert pending.pairing_id == "pairing-0002-abcdef" and pending.state == "staged"
    else:
        assert pending.pairing_id == _PAIRING and pending.state == "activating"
        assert pending_outbound_key(db) == _K_OUT


def test_update_app_secrets_rolls_everything_back_when_the_decision_raises(db: Path) -> None:
    from app.db.secrets import replace_app_secrets, update_app_secrets

    replace_app_secrets(db, {"row_a": "one"})

    def compute(read):  # type: ignore[no-untyped-def]
        assert read("row_a") == "one"
        raise RuntimeError("changed my mind")

    with pytest.raises(RuntimeError):
        update_app_secrets(db, lambda read: ({"row_a": "two", "row_b": "new"}, compute(read)))
    assert load_app_secret_text(db, "row_a") == "one"
    assert load_app_secret_text(db, "row_b") is None


def test_update_app_secrets_reads_see_current_rows_and_report_unreadable_ones_as_absent(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.secrets import replace_app_secrets, update_app_secrets

    replace_app_secrets(db, {"row_a": "one"})
    seen = update_app_secrets(db, lambda read: ({"row_b": "two"}, (read("row_a"), read("row_b"), read("missing"))))
    assert seen == ("one", None, None)
    assert load_app_secret_text(db, "row_b") == "two"

    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    assert update_app_secrets(db, lambda read: ({}, read("row_a"))) is None


def test_deleting_rows_works_without_a_configured_key_but_writing_does_not(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.db.secrets import SecretsConfigurationError, replace_app_secrets

    replace_app_secrets(db, {"row_a": "one"})
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(db.parent / "missing.json"))
    with pytest.raises(SecretsConfigurationError):
        replace_app_secrets(db, {"row_b": "two"})
    replace_app_secrets(db, {"row_a": None})
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    assert load_app_secret_text(db, "row_a") is None
