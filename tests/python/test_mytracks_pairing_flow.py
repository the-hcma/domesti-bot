"""Protocol 2 pairing flow against a fake My Tracks: every step can fail or lose its answer."""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet

from app.db.secrets import load_app_secret_text, save_mytracks_relay_api_key_to_db
from app.mytracks_pairing_flow import (
    ACTIVATION_ATTEMPTS,
    PairingRefusedError,
    PairingResult,
    reconcile_pairing,
    run_pairing_v2,
)
from app.mytracks_relay_keys import (
    PENDING_TTL_SECONDS,
    PROTOCOL_LEGACY,
    PROTOCOL_SPLIT,
    ROW_LEGACY_KEY,
    expire_stale_pending,
    load_state,
    outbound_key,
    pending_outbound_key,
    verify_inbound,
)
from app.mytracks_service import MyTracksAmbiguousError, MyTracksSyncError, StageResult

_LEGACY = "-".join(["shared", "legacy", "key", "1111111111"])
_KWARGS: dict[str, Any] = {
    "base_url": "https://tracks.example.test",
    "domesti_base_url": "https://bot.example.test",
    "user_location_test_url": "https://bot.example.test/v1/webhooks/location_update/test",
    "user_location_update_url": "https://bot.example.test/v1/webhooks/location_update",
    "username": "admin",
    "password": "pw",
}


class FakeMyTracks:
    """Stands in for ``MyTracksAdminSession``; ``script`` decides each answer, ``calls`` records them."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.stage_result: StageResult | Exception = StageResult(protocol_version=2, status="staged")
        self.probe_inbound_ok: bool | Exception = True
        self.activate_answers: list[str | Exception] = ["active"]
        self.state_answers: list[str | Exception] = ["active"]
        self.staged: dict[str, str] = {}

    # --- the session interface ---
    def __call__(self, base_url: str, *, username: str, password: str) -> FakeMyTracks:
        return self

    def __enter__(self) -> FakeMyTracks:
        return self

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        return None

    def stage_pairing(self, **kwargs: str) -> StageResult:
        self.calls.append("stage")
        self.staged = {"inbound": kwargs["inbound_key"], "outbound": kwargs["outbound_key"], "id": kwargs["pairing_id"]}
        if isinstance(self.stage_result, Exception):
            raise self.stage_result
        return self.stage_result

    def probe_inbound(self, pairing_id: str) -> bool:
        self.calls.append("probe_inbound")
        if isinstance(self.probe_inbound_ok, Exception):
            raise self.probe_inbound_ok
        return self.probe_inbound_ok

    def activate(self, pairing_id: str) -> str:
        self.calls.append("activate")
        answer = self.activate_answers.pop(0) if len(self.activate_answers) > 1 else self.activate_answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def state(self, pairing_id: str) -> str:
        self.calls.append("state")
        answer = self.state_answers.pop(0) if len(self.state_answers) > 1 else self.state_answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def abort(self, pairing_id: str) -> None:
        self.calls.append("abort")


_LOST = MyTracksAmbiguousError("answer lost")


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "ui.sqlite"


@pytest.fixture
def fake() -> FakeMyTracks:
    return FakeMyTracks()


def _run(db: Path, fake: FakeMyTracks, *, outbound_ok: bool = True, require_v2: bool = False) -> PairingResult:
    sleeps: list[float] = []
    with (
        patch("app.mytracks_pairing_flow.MyTracksAdminSession", fake),
        patch("app.mytracks_pairing_flow.probe_outbound_key", return_value=outbound_ok),
    ):
        result = run_pairing_v2(db, require_v2=require_v2, sleep=sleeps.append, **_KWARGS)
    fake.sleeps = sleeps  # type: ignore[attr-defined]
    return result


def _reconcile(db: Path, fake: FakeMyTracks) -> str:
    with patch("app.mytracks_pairing_flow.MyTracksAdminSession", fake):
        return reconcile_pairing(
            db, base_url="https://tracks.example.test", username="admin", password="pw", sleep=lambda _s: None
        )


# --- happy paths --------------------------------------------------------------------------------------------


def test_a_first_pairing_activates_with_a_key_per_direction(db: Path, fake: FakeMyTracks) -> None:
    result = _run(db, fake)

    assert result.activation == "active"
    assert result.protocol_version == 2
    assert fake.calls == ["stage", "probe_inbound", "activate"]
    state = load_state(db)
    assert state.protocol_version == PROTOCOL_SPLIT
    assert state.pending is None
    assert state.active_pairing_id == fake.staged["id"]
    assert outbound_key(db) == fake.staged["outbound"]
    assert verify_inbound(db, fake.staged["inbound"]) is True
    assert verify_inbound(db, fake.staged["outbound"]) is False, "the two directions use different keys"
    assert fake.staged["inbound"] != fake.staged["outbound"]


def test_repairing_over_a_shared_key_keeps_the_old_key_for_a_grace_only(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    _run(db, fake)
    state = load_state(db)
    assert load_app_secret_text(db, ROW_LEGACY_KEY) is None
    assert verify_inbound(db, _LEGACY, now=(state.previous_expires_at or 0) - 1) is True
    assert verify_inbound(db, _LEGACY, now=(state.previous_expires_at or 0) + 1) is False


def test_the_stage_response_never_contains_a_key_the_session_did_not_send_itself(db: Path, fake: FakeMyTracks) -> None:
    _run(db, fake)
    assert outbound_key(db) == fake.staged["outbound"]
    assert pending_outbound_key(db) is None


# --- failures before activation leave the previous pairing working ------------------------------------------


def _assert_previous_pairing_intact(db: Path) -> None:
    state = load_state(db)
    assert state.pending is None
    assert state.protocol_version == PROTOCOL_LEGACY
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == _LEGACY
    assert pending_outbound_key(db) is None
    assert outbound_key(db) == _LEGACY


def test_a_failed_stage_leaves_the_previous_pairing_untouched(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.stage_result = MyTracksSyncError("boom")
    with pytest.raises(MyTracksSyncError, match="boom"):
        _run(db, fake)
    _assert_previous_pairing_intact(db)
    assert "abort" in fake.calls


def test_a_failed_inbound_probe_aborts_both_sides_and_changes_nothing(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.probe_inbound_ok = False
    with pytest.raises(MyTracksSyncError, match="inbound key"):
        _run(db, fake)
    _assert_previous_pairing_intact(db)
    assert fake.calls == ["stage", "probe_inbound", "abort"]


def test_a_failed_outbound_probe_aborts_both_sides_and_changes_nothing(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    with pytest.raises(MyTracksSyncError, match="outbound key"):
        _run(db, fake, outbound_ok=False)
    _assert_previous_pairing_intact(db)
    assert fake.calls == ["stage", "probe_inbound", "abort"]


def test_an_unexpected_error_while_probing_also_aborts(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.probe_inbound_ok = RuntimeError("kaboom")
    with pytest.raises(RuntimeError):
        _run(db, fake)
    _assert_previous_pairing_intact(db)
    assert "abort" in fake.calls


# --- an old My Tracks ---------------------------------------------------------------------------------------


def test_an_old_my_tracks_that_adopts_the_inbound_key_is_followed_into_protocol_1(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.stage_result = StageResult(protocol_version=1, status="")

    result = _run(db, fake)

    assert result.protocol_version == 1
    assert "does not support relay protocol 2" in result.detail
    state = load_state(db)
    assert state.protocol_version == PROTOCOL_LEGACY and state.pending is None
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == fake.staged["inbound"], (
        "domesti-bot adopts the key My Tracks took"
    )
    assert outbound_key(db) == fake.staged["inbound"]
    assert fake.calls == ["stage"]


def test_with_protocol_2_required_an_old_my_tracks_is_refused_but_the_sides_stay_in_sync(
    db: Path, fake: FakeMyTracks
) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.stage_result = StageResult(protocol_version=1, status="")
    with pytest.raises(PairingRefusedError, match="required"):
        _run(db, fake, require_v2=True)
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == fake.staged["inbound"]
    assert load_state(db).pending is None


# --- the activation step ------------------------------------------------------------------------------------


def test_a_lost_activation_answer_is_retried_with_backoff_and_then_succeeds(db: Path, fake: FakeMyTracks) -> None:
    fake.activate_answers = [_LOST, _LOST, "active"]
    result = _run(db, fake)
    assert result.activation == "active"
    assert fake.calls.count("activate") == 3
    assert fake.sleeps == [2.0, 4.0]  # type: ignore[attr-defined]
    assert load_state(db).protocol_version == PROTOCOL_SPLIT


def test_when_every_activation_answer_is_lost_the_state_is_asked_and_may_promote(db: Path, fake: FakeMyTracks) -> None:
    fake.activate_answers = [_LOST]
    fake.state_answers = ["active"]
    result = _run(db, fake)
    assert result.activation == "active"
    assert fake.calls.count("activate") == ACTIVATION_ATTEMPTS
    assert fake.calls[-1] == "state"


def test_an_unreachable_my_tracks_leaves_the_pairing_activating_with_nothing_discarded(
    db: Path, fake: FakeMyTracks
) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]

    result = _run(db, fake)

    assert result.activation == "unconfirmed"
    assert "not confirmed" in result.detail
    state = load_state(db)
    assert state.pending is not None and state.pending.state == "activating"
    assert state.protocol_version == PROTOCOL_LEGACY, "still presenting and verifying the previous pairing"
    assert pending_outbound_key(db) == fake.staged["outbound"]
    assert verify_inbound(db, fake.staged["inbound"]) is True, "the staged inbound key keeps working"
    assert load_app_secret_text(db, ROW_LEGACY_KEY) == _LEGACY


def test_an_activating_pairing_survives_the_nominal_expiry_and_a_restart_then_reconciles(
    db: Path, fake: FakeMyTracks
) -> None:
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    pending = load_state(db).pending
    assert pending is not None
    far_future = pending.expires_at + 10 * PENDING_TTL_SECONDS
    assert expire_stale_pending(db, now=far_future) is False
    assert verify_inbound(db, fake.staged["inbound"], now=far_future) is True

    # "Restart": a fresh process asks My Tracks, which did activate.
    fake.state_answers = ["active"]
    assert _reconcile(db, fake) == "promoted"
    state = load_state(db)
    assert state.protocol_version == PROTOCOL_SPLIT and state.pending is None
    assert outbound_key(db) == fake.staged["outbound"]


def test_reconcile_retries_the_activation_when_my_tracks_still_has_it_staged(db: Path, fake: FakeMyTracks) -> None:
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    fake.state_answers = ["staged"]
    fake.activate_answers = ["active"]
    assert _reconcile(db, fake) == "promoted"
    assert load_state(db).protocol_version == PROTOCOL_SPLIT


def test_reconcile_discards_the_pairing_only_when_my_tracks_confirms_it_never_activated(
    db: Path, fake: FakeMyTracks
) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)

    fake.state_answers = ["unknown", "unknown"]
    assert _reconcile(db, fake) == "aborted"
    _assert_previous_pairing_intact(db)


def test_reconcile_stays_unconfirmed_while_my_tracks_is_unreachable(db: Path, fake: FakeMyTracks) -> None:
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    assert _reconcile(db, fake) == "unconfirmed"
    assert load_state(db).pending is not None


def test_reconcile_does_nothing_without_an_activating_pairing(db: Path, fake: FakeMyTracks) -> None:
    assert _reconcile(db, fake) == "none"
    _run(db, fake)
    assert _reconcile(db, fake) == "none"


def test_a_definite_unknown_answer_that_is_really_active_still_promotes(db: Path, fake: FakeMyTracks) -> None:
    fake.activate_answers = ["unknown"]
    fake.state_answers = ["active"]
    assert _run(db, fake).activation == "active"
    assert load_state(db).protocol_version == PROTOCOL_SPLIT


def test_a_definite_unknown_answer_confirmed_by_the_state_aborts_and_keeps_the_previous_pairing(
    db: Path, fake: FakeMyTracks
) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.activate_answers = ["expired"]
    fake.state_answers = ["unknown"]
    with pytest.raises(MyTracksSyncError, match="did not activate"):
        _run(db, fake)
    _assert_previous_pairing_intact(db)


def test_a_local_failure_while_promoting_leaves_the_pairing_activating_not_lost(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    with patch("app.mytracks_pairing_flow.promote_pending", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            _run(db, fake)
    state = load_state(db)
    assert state.pending is not None and state.pending.state == "activating"
    assert pending_outbound_key(db) == fake.staged["outbound"]
    # Once the disk recovers, reconciling completes the switch.
    fake.state_answers = ["active"]
    assert _reconcile(db, fake) == "promoted"


def test_the_activating_state_is_persisted_before_the_activation_is_sent(db: Path, fake: FakeMyTracks) -> None:
    seen: list[str] = []
    original = fake.activate

    def spy(pairing_id: str) -> str:
        pending = load_state(db).pending
        seen.append(pending.state if pending else "none")
        return original(pairing_id)

    fake.activate = spy  # type: ignore[method-assign]
    _run(db, fake)
    assert seen == ["activating"]


# --- review fixes -------------------------------------------------------------------------------------------


def test_a_new_pairing_cannot_replace_one_that_is_activating(db: Path, fake: FakeMyTracks) -> None:
    from app.mytracks_relay_keys import PairingInProgressError

    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    first = dict(fake.staged)
    pending_before = load_state(db).pending
    fake.calls.clear()

    with pytest.raises(PairingInProgressError):
        _run(db, fake)

    assert fake.calls == [], "nothing is sent to My Tracks for the refused pairing"
    assert load_state(db).pending == pending_before
    assert pending_outbound_key(db) == first["outbound"]
    assert verify_inbound(db, first["inbound"]) is True


def test_a_promotion_that_did_not_happen_is_not_reported_as_success(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    with patch("app.mytracks_pairing_flow.promote_pending", return_value=False):
        result = _run(db, fake)
    assert result.activation == "unconfirmed"
    assert load_state(db).protocol_version == PROTOCOL_LEGACY


def test_a_pairing_replaced_underneath_is_not_activated(db: Path, fake: FakeMyTracks) -> None:
    from app.mytracks_relay_keys import set_pending_state as real_set

    def replaced(path: Path, pairing_id: str, new_state: str) -> object:
        return None if new_state == "activating" else real_set(path, pairing_id, new_state)  # type: ignore[arg-type]

    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    with patch("app.mytracks_pairing_flow.set_pending_state", side_effect=replaced):
        with pytest.raises(MyTracksSyncError, match="replaced before it could be activated"):
            _run(db, fake)
    assert "activate" not in fake.calls
    assert "abort" in fake.calls


def test_a_pairing_replaced_while_probing_stops_before_probing(db: Path, fake: FakeMyTracks) -> None:
    with patch("app.mytracks_pairing_flow.set_pending_state", return_value=None):
        with pytest.raises(MyTracksSyncError, match="replaced while it was being verified"):
            _run(db, fake)
    assert "probe_inbound" not in fake.calls


def test_a_rejected_activation_discards_the_staged_pairing_and_keeps_the_previous_one(
    db: Path, fake: FakeMyTracks
) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.activate_answers = [MyTracksSyncError("My Tracks rejected the admin session during activation")]
    with pytest.raises(MyTracksSyncError, match="rejected the admin session"):
        _run(db, fake)
    _assert_previous_pairing_intact(db)
    assert "abort" in fake.calls


def test_a_malformed_protocol_2_stage_answer_is_an_error_not_a_legacy_fallback(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.stage_result = StageResult(protocol_version=2, status="weird")
    with pytest.raises(MyTracksSyncError, match="status 'weird'"):
        _run(db, fake)
    _assert_previous_pairing_intact(db)


def test_cleanup_failures_do_not_mask_the_original_error(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.probe_inbound_ok = False

    def broken_abort(pairing_id: str) -> None:
        raise OSError("network down")

    fake.abort = broken_abort  # type: ignore[method-assign]
    with patch("app.mytracks_pairing_flow.abort_pending", side_effect=OSError("disk full")):
        with pytest.raises(MyTracksSyncError, match="inbound key"):
            _run(db, fake)


def test_only_one_pairing_or_reconcile_runs_at_a_time(db: Path, fake: FakeMyTracks) -> None:
    from app.mytracks_pairing_flow import _PAIRING_LOCK, PairingBusyError

    assert _PAIRING_LOCK.acquire(blocking=False)
    try:
        with pytest.raises(PairingBusyError):
            _run(db, fake)
        with pytest.raises(PairingBusyError):
            _reconcile(db, fake)
    finally:
        _PAIRING_LOCK.release()
    assert fake.calls == []
    assert _run(db, fake).activation == "active", "the lock is released afterwards"


def _drop_staged_outbound(db: Path) -> None:
    from app.db.secrets import replace_app_secrets
    from app.mytracks_relay_keys import ROW_OUTBOUND_PENDING

    replace_app_secrets(db, {ROW_OUTBOUND_PENDING: None})


def test_reconcile_with_the_staged_outbound_key_missing_asks_my_tracks_first(db: Path, fake: FakeMyTracks) -> None:
    save_mytracks_relay_api_key_to_db(db, _LEGACY)
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    _drop_staged_outbound(db)

    fake.state_answers = ["unknown"]
    assert _reconcile(db, fake) == "aborted"
    assert load_state(db).pending is None
    assert outbound_key(db) == _LEGACY


def test_reconcile_with_the_staged_outbound_key_missing_and_my_tracks_active_says_to_pair_again(
    db: Path, fake: FakeMyTracks
) -> None:
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    _drop_staged_outbound(db)

    fake.state_answers = ["active"]
    with pytest.raises(MyTracksSyncError, match="Pair again"):
        _reconcile(db, fake)
    assert load_state(db).pending is None, "dropped so that a new pairing is possible"


def test_reconcile_with_stored_credentials_only_acts_with_a_saved_password(db: Path, fake: FakeMyTracks) -> None:
    from app.mytracks_pairing_flow import reconcile_with_stored_credentials

    def run(password: str | None) -> str:
        with patch("app.mytracks_pairing_flow.MyTracksAdminSession", fake):
            return reconcile_with_stored_credentials(
                db,
                base_url="https://tracks.example.test",
                username="admin",
                load_password=lambda _path: password,
            )

    assert run("pw") == "none"
    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)
    assert run(None) == "unconfirmed"
    assert run("") == "unconfirmed"
    fake.state_answers = ["active"]
    assert run("pw") == "promoted"
    assert load_state(db).protocol_version == PROTOCOL_SPLIT


def test_reconcile_with_stored_credentials_survives_an_undecryptable_password(db: Path, fake: FakeMyTracks) -> None:
    from app.db.secrets import SecretsDecryptError
    from app.mytracks_pairing_flow import reconcile_with_stored_credentials

    fake.activate_answers = [_LOST]
    fake.state_answers = [_LOST]
    _run(db, fake)

    def broken(_path: Path) -> str | None:
        raise SecretsDecryptError("lost key")

    with patch("app.mytracks_pairing_flow.MyTracksAdminSession", fake):
        assert (
            reconcile_with_stored_credentials(
                db, base_url="https://tracks.example.test", username="admin", load_password=broken
            )
            == "unconfirmed"
        )
