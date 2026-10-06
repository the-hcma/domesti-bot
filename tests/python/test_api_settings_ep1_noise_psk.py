"""EP1 Noise pre-shared key settings: the key is write-only and never returned."""

from __future__ import annotations

import argparse
import logging
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.db.secrets import load_ep1_noise_psk_from_db, save_ep1_noise_psk_to_db
from app.ep1_credentials import resolve_ep1_noise_psk

_PSK = "-".join(["SENTINEL", "ep1", "noise", "psk"])


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EP1_NOISE_PSK", raising=False)
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))


def _client(*, cache_path: Path | None, cli_psk: str | None = None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        ep1_noise_psk=cli_psk,
        tailwind_token=None,
        vizio_auth_token=None,
        vizio_host=[],
        no_vizio=False,
    )
    app = create_app(args)
    return TestClient(app), app


def test_put_then_get_never_returns_the_key(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    put = client.put("/v1/settings/ep1-noise-psk", json={"noise_psk": _PSK})
    assert put.status_code == HTTPStatus.OK
    assert load_ep1_noise_psk_from_db(db) == _PSK
    get = client.get("/v1/settings/ep1-noise-psk")
    body = get.json()
    assert body["configured"] is True
    assert body["source"] == "database"
    assert body["stored_in_database"] is True
    assert "stored_noise_psk" not in body
    assert _PSK not in get.text
    assert isinstance(body["updated_at"], float)


def test_updated_at_is_none_until_a_key_is_stored_and_after_it_is_cleared(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    assert client.get("/v1/settings/ep1-noise-psk").json()["updated_at"] is None
    assert client.put("/v1/settings/ep1-noise-psk", json={"noise_psk": _PSK}).status_code == HTTPStatus.OK
    assert client.delete("/v1/settings/ep1-noise-psk").status_code == HTTPStatus.OK
    body = client.get("/v1/settings/ep1-noise-psk").json()
    assert body["updated_at"] is None
    assert body["stored_in_database"] is False


def test_env_key_overrides_the_database_row_without_returning_either(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("EP1_NOISE_PSK", "env-psk-value-1234")
    client, _app = _client(cache_path=db)
    response = client.get("/v1/settings/ep1-noise-psk")
    body = response.json()
    assert body["source"] == "env"
    assert body["stored_in_database"] is True
    assert isinstance(body["updated_at"], float)
    assert _PSK not in response.text
    assert "env-psk-value-1234" not in response.text


def test_an_undecryptable_row_is_reported_as_stored_but_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=db)
    response = client.get("/v1/settings/ep1-noise-psk")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["stored_in_database"] is True
    assert body["configured"] is False
    assert body["source"] == "none"


def _remember_ep1(db: Path) -> None:
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )


def _fake_ep1_client() -> MagicMock:
    info = MagicMock()
    info.friendly_name = "EP1"
    info.name = "ep1"
    fake = MagicMock()
    fake.connect = AsyncMock()
    fake.device_info = AsyncMock(return_value=info)
    fake.disconnect = AsyncMock()
    return fake


def test_a_blank_test_uses_the_stored_key_on_the_server(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    _remember_ep1(db)
    save_ep1_noise_psk_to_db(db, _PSK)
    client, _app = _client(cache_path=db)
    with patch("app.settings_credentials_test.APIClient", return_value=_fake_ep1_client()) as api_client:
        response = client.post("/v1/settings/ep1-noise-psk/test", json={"device_id": "28:05:a5:28:c8:48"})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "database"
    assert api_client.call_args.kwargs["noise_psk"] == _PSK
    assert _PSK not in response.text


def test_a_blank_test_with_no_stored_key_still_works_for_plaintext_firmware(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    _remember_ep1(db)
    client, _app = _client(cache_path=db)
    with patch("app.settings_credentials_test.APIClient", return_value=_fake_ep1_client()) as api_client:
        response = client.post("/v1/settings/ep1-noise-psk/test", json={"device_id": "28:05:a5:28:c8:48"})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert api_client.call_args.kwargs["noise_psk"] is None
    assert "plaintext" in body["detail"].lower()


def test_a_blank_test_with_an_undecryptable_row_explains_the_key_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    _remember_ep1(db)
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=db)
    response = client.post("/v1/settings/ep1-noise-psk/test", json={"device_id": "28:05:a5:28:c8:48"})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "cannot be decrypted" in response.json()["detail"]


def test_env_key_wins_over_an_undecryptable_row_for_status_and_test(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    _remember_ep1(db)
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setenv("EP1_NOISE_PSK", "env-psk-value-1234")
    client, _app = _client(cache_path=db)
    status = client.get("/v1/settings/ep1-noise-psk").json()
    assert (status["configured"], status["source"], status["stored_in_database"]) == (True, "env", True)
    with patch("app.settings_credentials_test.APIClient", return_value=_fake_ep1_client()) as api_client:
        response = client.post("/v1/settings/ep1-noise-psk/test", json={"device_id": "28:05:a5:28:c8:48"})
    assert response.status_code == HTTPStatus.OK
    assert response.json()["source"] == "env"
    assert api_client.call_args.kwargs["noise_psk"] == "env-psk-value-1234"


def test_cli_key_is_reported_as_the_source_and_wins_over_the_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=db, cli_psk="cli-psk-value-1234")
    body = client.get("/v1/settings/ep1-noise-psk").json()
    assert (body["configured"], body["source"]) == (True, "cli")


def test_resolver_ignores_an_undecryptable_row_and_logs_why(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    save_ep1_noise_psk_to_db(db, _PSK)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.ep1_credentials"):
        assert resolve_ep1_noise_psk(cli_psk=None, cache_path=db) == ("", "none")
    assert "cannot be decrypted" in caplog.text
    assert _PSK not in caplog.text


def test_put_without_a_cache_is_a_conflict(tmp_path: Path) -> None:
    client, _app = _client(cache_path=None)
    response = client.put("/v1/settings/ep1-noise-psk", json={"noise_psk": _PSK})
    assert response.status_code == HTTPStatus.CONFLICT


def test_put_without_a_secrets_key_is_unavailable(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing-config.json"))
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.put("/v1/settings/ep1-noise-psk", json={"noise_psk": _PSK})
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert _PSK not in response.text
