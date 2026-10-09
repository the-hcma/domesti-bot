"""The pair route with relay protocol 2: pre-flight, the require-v2 setting, unconfirmed activation, reconcile."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.secrets import load_mytracks_relay_api_key_from_db
from app.mytracks_pairing_flow import PairingRefusedError, PairingResult
from app.mytracks_relay_keys import (
    ROW_OUTBOUND,
    ROW_STATE,
    PendingPairing,
    RelayState,
    load_state,
    stage_pairing,
)
from app.mytracks_service import DomestiBotConfigFromMyTracks, MyTracksPairResult, MyTracksSyncError
from app.mytracks_store import load_require_relay_protocol_2

_PAIR_BODY = {"domain": "https://tracks.example.com", "username": "admin", "password": "secret-pw"}
_PAIR_OK = MyTracksPairResult(status_code=HTTPStatus.OK)
_V2_CONFIG = DomestiBotConfigFromMyTracks(protocol_version=2)
_V1_CONFIG = DomestiBotConfigFromMyTracks(protocol_version=None)
_K_IN = "-".join(["inbound", "relay", "key", "0123456789"])
_K_OUT = "-".join(["outbound", "relay", "key", "9876543210"])
_PAIRING = "pairing-0001-abcdef"


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "ui.sqlite"


@pytest.fixture
def client(db: Path) -> TestClient:
    return TestClient(create_app(argparse.Namespace(discovery_cache=str(db), tailwind_token=None)))


def _fake_v2_pairing(db: Path, *, activation: str = "active", detail: str = "") -> PairingResult:
    """Stand in for the network flow but do its local part: stage and, when activated, promote."""
    from app.mytracks_relay_keys import promote_pending

    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    if activation == "active":
        promote_pending(db, _PAIRING)
    else:
        from app.mytracks_relay_keys import set_pending_state

        set_pending_state(db, _PAIRING, "activating")
    return PairingResult(pairing_id=_PAIRING, activation=activation, detail=detail)  # type: ignore[arg-type]


def _pair(client: TestClient, *, config: DomestiBotConfigFromMyTracks | Exception = _V2_CONFIG, flow: object = None):
    flow_patch = (
        patch("app.api.mytracks_routes.run_pairing_v2", side_effect=flow)
        if flow is not None
        else patch("app.api.mytracks_routes.run_pairing_v2")
    )
    with (
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", side_effect=[config, config])
        if isinstance(config, Exception)
        else patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", return_value=config),
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK) as legacy,
        flow_patch as run,
    ):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    return response, legacy, run


# --- choosing the protocol ----------------------------------------------------------------------------------


def test_a_my_tracks_that_supports_protocol_2_is_paired_with_protocol_2(db: Path, client: TestClient) -> None:
    response, legacy, _run = _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    assert response.status_code == HTTPStatus.OK
    legacy.assert_not_called()
    body = response.json()
    assert body["relay_protocol_version"] == 2
    assert body["relay_pairing_state"] == "active"
    assert body["relay_key_configured"] is True
    assert body["relay_key_updated_at"] is not None
    assert _K_IN not in response.text and _K_OUT not in response.text
    assert load_mytracks_relay_api_key_from_db(db) is None


def test_an_old_my_tracks_is_paired_with_one_shared_key_as_before_and_the_panel_says_version_1(
    db: Path, client: TestClient
) -> None:
    response, legacy, run = _pair(client, config=_V1_CONFIG)
    assert response.status_code == HTTPStatus.OK
    legacy.assert_called_once()
    run.assert_not_called()
    body = response.json()
    assert body["relay_protocol_version"] == 1
    assert body["relay_pairing_state"] == "none"
    assert body["relay_key_configured"] is True
    assert load_mytracks_relay_api_key_from_db(db) is not None


def test_a_my_tracks_that_cannot_be_asked_falls_back_to_the_legacy_request_which_reports_the_real_error(
    client: TestClient,
) -> None:
    with (
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", side_effect=MyTracksSyncError("login failed")),
        patch("app.api.mytracks_routes.pair_with_my_tracks", side_effect=MyTracksSyncError("bad password")),
    ):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert "bad password" in response.json()["detail"]


# --- the require-version-2 setting --------------------------------------------------------------------------


def _save_settings(client: TestClient) -> None:
    assert (
        client.put(
            "/v1/settings/my-tracks", json={"domain": "https://tracks.example.com", "username": "admin"}
        ).status_code
        == 200
    )


def test_the_requirement_needs_saved_settings_then_persists_and_shows_in_the_status(
    db: Path, client: TestClient
) -> None:
    refused = client.patch("/v1/settings/my-tracks/relay-protocol", json={"require_protocol_2": True})
    assert refused.status_code == HTTPStatus.CONFLICT

    _save_settings(client)
    ok = client.patch("/v1/settings/my-tracks/relay-protocol", json={"require_protocol_2": True})
    assert ok.status_code == HTTPStatus.OK
    assert ok.json()["require_relay_protocol_2"] is True
    assert load_require_relay_protocol_2(db) is True
    off = client.patch("/v1/settings/my-tracks/relay-protocol", json={"require_protocol_2": False})
    assert off.json()["require_relay_protocol_2"] is False


def test_with_protocol_2_required_an_old_my_tracks_is_refused_before_any_key_is_sent(
    db: Path, client: TestClient
) -> None:
    _save_settings(client)
    client.patch("/v1/settings/my-tracks/relay-protocol", json={"require_protocol_2": True})
    response, legacy, run = _pair(client, config=_V1_CONFIG)
    assert response.status_code == HTTPStatus.CONFLICT
    assert "nothing was changed" in response.json()["detail"]
    legacy.assert_not_called()
    run.assert_not_called()
    assert load_mytracks_relay_api_key_from_db(db) is None
    status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert "protocol 2 is required" in status["last_pair_error"].lower() or "required" in status["last_pair_error"]


def test_with_protocol_2_required_an_unreachable_my_tracks_is_reported_not_silently_downgraded(
    client: TestClient,
) -> None:
    _save_settings(client)
    client.patch("/v1/settings/my-tracks/relay-protocol", json={"require_protocol_2": True})
    with (
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", side_effect=MyTracksSyncError("login failed")),
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK) as legacy,
    ):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    legacy.assert_not_called()


def test_a_refusal_from_the_flow_is_a_409_with_its_message(client: TestClient) -> None:
    _save_settings(client)
    response, _legacy, _run = _pair(client, flow=PairingRefusedError("version 2 required"))
    assert response.status_code == HTTPStatus.CONFLICT
    assert response.json()["detail"] == "version 2 required"


# --- an unconfirmed activation and reconcile ----------------------------------------------------------------


def test_an_unconfirmed_activation_is_a_202_that_shows_activating_and_keeps_the_error(
    db: Path, client: TestClient
) -> None:
    response, _legacy, _run = _pair(
        client, flow=lambda *a, **k: _fake_v2_pairing(db, activation="unconfirmed", detail="not confirmed yet")
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    body = response.json()
    assert body["relay_pairing_state"] == "activating"
    assert body["relay_protocol_version"] == 1
    assert body["last_pair_error"] == "not confirmed yet"
    assert body["paired_at"] is None, "an unconfirmed pairing is not recorded as paired"


def test_reconcile_promotes_a_confirmed_pairing_and_records_it_as_paired(db: Path, client: TestClient) -> None:
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db, activation="unconfirmed", detail="not confirmed yet"))
    with (
        patch("app.api.mytracks_routes.reconcile_pairing", return_value="promoted") as reconcile,
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", return_value=_V2_CONFIG),
    ):
        response = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "secret-pw"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["result"] == "promoted"
    assert response.json()["status"]["paired_at"] is not None
    assert response.json()["status"]["last_pair_error"] is None
    assert reconcile.call_args.kwargs["username"] == "admin"


@pytest.mark.parametrize("result", ["none", "aborted", "unconfirmed"])
def test_reconcile_results_that_do_not_pair_are_reported_as_is(client: TestClient, result: str) -> None:
    _save_settings(client)
    with patch("app.api.mytracks_routes.reconcile_pairing", return_value=result):
        response = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "secret-pw"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["result"] == result


def test_reconcile_needs_saved_settings_and_a_password(client: TestClient) -> None:
    assert (
        client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"}).status_code == HTTPStatus.CONFLICT
    )
    _save_settings(client)
    assert client.post("/v1/settings/my-tracks/pair/reconcile", json={}).status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    with patch("app.api.mytracks_routes.reconcile_pairing", side_effect=MyTracksSyncError("unreachable")):
        failed = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"})
    assert failed.status_code == HTTPStatus.BAD_GATEWAY


def test_pairing_again_settles_an_earlier_unconfirmed_pairing_first(db: Path, client: TestClient) -> None:
    with patch("app.api.mytracks_routes.reconcile_pairing", return_value="promoted") as reconcile:
        response, _legacy, _run = _pair(client, config=_V1_CONFIG)
    assert response.status_code == HTTPStatus.OK
    reconcile.assert_called_once()


# --- unpairing and the status -------------------------------------------------------------------------------


def test_unpairing_removes_every_relay_credential_including_protocol_2_rows(db: Path, client: TestClient) -> None:
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    assert load_state(db).protocol_version == 2
    response = client.delete("/v1/settings/my-tracks/pair")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["relay_key_configured"] is False
    assert body["relay_protocol_version"] == 1
    assert body["relay_pairing_state"] == "none"
    assert load_state(db) == RelayState()
    from app.db.secrets import app_secret_stored

    assert app_secret_stored(db, ROW_OUTBOUND) is False and app_secret_stored(db, ROW_STATE) is False


def test_a_pending_pairing_is_reported_by_its_stage(db: Path, client: TestClient) -> None:
    _save_settings(client)
    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert status["relay_pairing_state"] == "staged"
    pending = load_state(db).pending
    assert isinstance(pending, PendingPairing)


def test_the_status_never_contains_a_relay_key(db: Path, client: TestClient) -> None:
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    text = client.get("/v1/settings/my-tracks/pair-status").text
    assert _K_IN not in text and _K_OUT not in text


# --- review fixes -------------------------------------------------------------------------------------------


def _activating(db: Path) -> None:
    from app.mytracks_relay_keys import set_pending_state

    stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    set_pending_state(db, _PAIRING, "activating")


def test_pairing_again_while_an_activation_is_unconfirmed_is_refused_and_replaces_nothing(
    db: Path, client: TestClient
) -> None:
    _save_settings(client)
    _activating(db)
    before = load_state(db).pending
    with (
        patch("app.api.mytracks_routes.reconcile_pairing", return_value="unconfirmed"),
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", return_value=_V2_CONFIG),
        patch("app.api.mytracks_routes.run_pairing_v2") as run,
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK) as legacy,
    ):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert response.status_code == HTTPStatus.CONFLICT
    assert "still waiting for My Tracks" in response.json()["detail"]
    run.assert_not_called()
    legacy.assert_not_called()
    assert load_state(db).pending == before


def test_an_unsettleable_earlier_pairing_is_a_409_with_the_way_out_not_a_502(db: Path, client: TestClient) -> None:
    _save_settings(client)
    _activating(db)
    with patch("app.api.mytracks_routes.reconcile_pairing", side_effect=MyTracksSyncError("state endpoint missing")):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert response.status_code == HTTPStatus.CONFLICT
    assert "Check activation, or reset the pairing" in response.json()["detail"]
    reset = client.delete("/v1/settings/my-tracks/pair")
    assert reset.status_code == HTTPStatus.OK
    assert load_state(db).pending is None


def test_a_busy_or_in_progress_pairing_is_a_409(client: TestClient) -> None:
    from app.mytracks_pairing_flow import PairingBusyError
    from app.mytracks_relay_keys import PairingInProgressError

    for error in (PairingBusyError("busy"), PairingInProgressError("in progress")):
        response, _legacy, _run = _pair(client, flow=error)
        assert response.status_code == HTTPStatus.CONFLICT
    with patch("app.api.mytracks_routes.reconcile_pairing", side_effect=PairingBusyError("busy")):
        _save_settings(client)
        assert client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"}).status_code == 409


def test_a_protocol_2_pairing_is_never_downgraded_because_my_tracks_could_not_be_asked(
    db: Path, client: TestClient
) -> None:
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    assert load_state(db).protocol_version == 2
    with (
        patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", side_effect=MyTracksSyncError("timeout")),
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK) as legacy,
    ):
        response = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    legacy.assert_not_called()
    assert load_state(db).protocol_version == 2


def test_pairing_recovers_when_the_stored_relay_rows_can_no_longer_be_decrypted(
    db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))  # the old key is lost
    response, legacy, _run = _pair(client, config=_V1_CONFIG)
    assert response.status_code == HTTPStatus.OK, response.text
    legacy.assert_called_once()
    assert response.json()["relay_protocol_version"] == 1


def test_an_expired_staged_pairing_reads_as_none_in_the_status(db: Path, client: TestClient) -> None:
    _save_settings(client)
    pending = stage_pairing(db, pairing_id=_PAIRING, inbound_key=_K_IN, outbound_key=_K_OUT)
    with patch("app.mytracks_store.pending_is_usable", return_value=False):
        status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert status["relay_pairing_state"] == "none"
    assert pending.state == "staged"


def test_the_status_leaks_neither_the_pepper_nor_a_verifier(db: Path, client: TestClient) -> None:
    from app.mytracks_relay_keys import load_pepper

    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db))
    pepper = load_pepper(db) or b""
    import base64

    state = load_state(db)
    text = client.get("/v1/settings/my-tracks/pair-status").text
    assert base64.b64encode(pepper).decode() not in text
    assert state.inbound_verifier not in text


@pytest.mark.asyncio
async def test_startup_reconcile_settles_an_activating_pairing_with_the_saved_password(
    db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import app as app_module
    from app.db.secrets import save_mytracks_admin_password_to_db

    _save_settings(client)
    _activating(db)
    save_mytracks_admin_password_to_db(db, "stored-pw")
    monkeypatch.setattr(app_module.runtime, "discovery_cache_path", lambda: db)
    with patch("app.mytracks_pairing_flow.reconcile_pairing", return_value="promoted") as reconcile:
        await app_module._reconcile_pairing_on_start()
    reconcile.assert_called_once()
    assert reconcile.call_args.kwargs["password"] == "stored-pw"
    assert reconcile.call_args.kwargs["username"] == "admin"


@pytest.mark.asyncio
async def test_startup_reconcile_never_raises(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.api import app as app_module

    monkeypatch.setattr(app_module.runtime, "discovery_cache_path", lambda: db)
    with patch("app.api.app.load_mytracks_config", side_effect=RuntimeError("db gone")):
        await app_module._reconcile_pairing_on_start()
    monkeypatch.setattr(app_module.runtime, "discovery_cache_path", lambda: None)
    await app_module._reconcile_pairing_on_start()


# --- the flow runs off the event loop; startup completes the record -----------------------------------------


@pytest.mark.asyncio
async def test_pairing_does_not_block_the_event_loop_so_my_tracks_can_call_back_during_the_probe(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import threading
    import time

    import httpx

    app = create_app(argparse.Namespace(discovery_cache=str(db), tailwind_token=None))
    release = threading.Event()
    entered = threading.Event()

    def slow_flow(*_args: object, **_kwargs: object) -> PairingResult:
        entered.set()
        # Stands in for the probe: blocks this thread until the main loop has answered a request, which is what
        # My Tracks's callback to our webhook URL needs. On the event loop thread this would deadlock.
        assert release.wait(timeout=10), "the event loop never served a request while pairing ran"
        return _fake_v2_pairing(db)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with (
            patch("app.api.mytracks_routes.fetch_mytracks_domesti_config", return_value=_V2_CONFIG),
            patch("app.api.mytracks_routes.run_pairing_v2", side_effect=slow_flow),
        ):
            pairing = asyncio.create_task(client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY))
            deadline = time.monotonic() + 10
            while not entered.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert entered.is_set()
            health = await client.get("/health")  # answered while the pairing flow is still running
            assert health.status_code == HTTPStatus.OK
            assert not pairing.done()
            release.set()
            response = await pairing
    assert response.status_code == HTTPStatus.OK, response.text


@pytest.mark.asyncio
async def test_startup_reconcile_that_promotes_completes_the_pairing_record(
    db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import app as app_module
    from app.db.secrets import save_mytracks_admin_password_to_db

    response, _legacy, _run = _pair(
        client, flow=lambda *a, **k: _fake_v2_pairing(db, activation="unconfirmed", detail="not confirmed yet")
    )
    assert response.status_code == HTTPStatus.ACCEPTED
    before = client.get("/v1/settings/my-tracks/pair-status").json()
    assert before["paired_at"] is None
    assert before["user_location_update_url"], "the intended URLs are remembered for the first pairing"
    assert before["last_pair_error"] == "not confirmed yet"

    save_mytracks_admin_password_to_db(db, "stored-pw")
    monkeypatch.setattr(app_module.runtime, "discovery_cache_path", lambda: db)
    with patch("app.mytracks_pairing_flow.reconcile_pairing", return_value="promoted"):
        await app_module._reconcile_pairing_on_start()

    after = client.get("/v1/settings/my-tracks/pair-status").json()
    assert after["paired_at"] is not None
    assert after["last_pair_error"] is None
    assert after["user_location_update_url"] == before["user_location_update_url"]


def test_an_unconfirmed_repairing_leaves_an_existing_pairings_urls_alone(db: Path, client: TestClient) -> None:
    _pair(client, config=_V1_CONFIG)  # an existing protocol 1 pairing
    before = client.get("/v1/settings/my-tracks/pair-status").json()
    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db, activation="unconfirmed", detail="not confirmed yet"))
    after = client.get("/v1/settings/my-tracks/pair-status").json()
    assert after["paired_at"] == before["paired_at"]
    assert after["user_location_update_url"] == before["user_location_update_url"]


# --- review fixes (domain normalization, discarded-pairing message) -----------------------------------------


def test_reconcile_uses_the_normalized_domain_even_when_a_bare_host_was_saved(db: Path, client: TestClient) -> None:
    assert (
        client.put("/v1/settings/my-tracks", json={"domain": "tracks.example.com", "username": "admin"}).status_code
        == 200
    )
    with patch("app.api.mytracks_routes.reconcile_pairing", return_value="none") as reconcile:
        response = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"})
    assert response.status_code == HTTPStatus.OK
    assert reconcile.call_args.kwargs["base_url"] == "https://tracks.example.com"


def test_reconcile_that_discards_the_pairing_replaces_the_stale_error_with_what_happened(
    db: Path, client: TestClient
) -> None:
    from app.mytracks_pairing_flow import DISCARDED_PAIRING_MESSAGE

    _pair(client, flow=lambda *a, **k: _fake_v2_pairing(db, activation="unconfirmed", detail="not confirmed yet"))
    assert client.get("/v1/settings/my-tracks/pair-status").json()["last_pair_error"] == "not confirmed yet"
    with patch("app.api.mytracks_routes.reconcile_pairing", return_value="aborted"):
        response = client.post("/v1/settings/my-tracks/pair/reconcile", json={"password": "x"})
    assert response.json()["result"] == "aborted"
    assert response.json()["status"]["last_pair_error"] == DISCARDED_PAIRING_MESSAGE
    assert "Pair again" in DISCARDED_PAIRING_MESSAGE


@pytest.mark.asyncio
async def test_startup_reconcile_uses_the_normalized_domain_and_records_a_discarded_pairing(
    db: Path, client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.api import app as app_module
    from app.mytracks_pairing_flow import DISCARDED_PAIRING_MESSAGE

    client.put("/v1/settings/my-tracks", json={"domain": "tracks.example.com", "username": "admin"})
    _activating(db)
    monkeypatch.setattr(app_module.runtime, "discovery_cache_path", lambda: db)
    with patch("app.mytracks_pairing_flow.reconcile_pairing", return_value="aborted") as reconcile:
        from app.db.secrets import save_mytracks_admin_password_to_db

        save_mytracks_admin_password_to_db(db, "stored-pw")
        await app_module._reconcile_pairing_on_start()
    assert reconcile.call_args.kwargs["base_url"] == "https://tracks.example.com"
    assert client.get("/v1/settings/my-tracks/pair-status").json()["last_pair_error"] == DISCARDED_PAIRING_MESSAGE
