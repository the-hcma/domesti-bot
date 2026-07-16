"""Tests for Kasa KLAP credentials settings routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.secrets import (
    kasa_credentials_stored_in_db,
    load_kasa_credentials_from_db,
    save_kasa_credentials_to_db,
)
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def test_get_kasa_credentials_reports_env_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/kasa-credentials")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "env"
    assert body["stored_in_database"] is False
    assert body["stored_password"] is None


def test_put_kasa_credentials_persists_and_returns_stored_password_on_get(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    r = client.put(
        "/v1/settings/kasa-credentials",
        json={"username": "alice@example.com", "password": "hunter2"},
    )
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "database"
    assert "password" not in body

    get_r = client.get("/v1/settings/kasa-credentials")
    get_body = get_r.json()
    assert get_body["stored_in_database"] is True
    assert get_body["stored_username"] == "alice@example.com"
    assert get_body["stored_password"] == "hunter2"
    assert get_body["password_stored"] is True
    assert "password" not in get_body
    assert load_kasa_credentials_from_db(db) == ("alice@example.com", "hunter2")


def test_get_kasa_credentials_decrypt_error_returns_null_stored_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", key)
    db = tmp_path / "ui.sqlite"
    save_kasa_credentials_to_db(db, username="alice@example.com", password="hunter2")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=db)
    r = client.get("/v1/settings/kasa-credentials")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["password_stored"] is True
    assert body["stored_password"] is None
    assert body["stored_username"] is None


def test_put_kasa_credentials_without_secrets_key_returns_503(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing-config.json"))
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.put(
        "/v1/settings/kasa-credentials",
        json={"username": "alice@example.com", "password": "hunter2"},
    )
    assert r.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert "domesti-bot.config.json" in r.json()["detail"]


def test_put_kasa_credentials_without_cache_returns_409(tmp_path: Path) -> None:
    client, _app = _client(cache_path=None)
    r = client.put(
        "/v1/settings/kasa-credentials",
        json={"username": "alice@example.com", "password": "hunter2"},
    )
    assert r.status_code == HTTPStatus.CONFLICT
    assert "discovery cache" in r.json()["detail"].lower()


def test_delete_kasa_credentials_clears_database_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_kasa_credentials_to_db(db, username="alice@example.com", password="hunter2")
    client, _app = _client(cache_path=db)
    r = client.delete("/v1/settings/kasa-credentials")
    assert r.status_code == HTTPStatus.OK
    assert r.json()["configured"] is False
    assert r.json()["stored_in_database"] is False
    assert kasa_credentials_stored_in_db(db) is False


def test_delete_kasa_credentials_noop_preserves_in_memory_session_creds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Clear with nothing stored must not wipe REPL-only session credentials."""
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    mgr.set_credentials(username="alice@example.com", password="hunter2")
    mgr.rediscover = AsyncMock()  # type: ignore[method-assign]
    state = DeviceManagersState(
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(discovery_cache=str(db)),
    )
    with (
        runtime.temporary_device_state(state),
        patch.object(runtime, "restart_device_state_watchers", new_callable=AsyncMock),
    ):
        r = client.delete("/v1/settings/kasa-credentials")
        assert r.status_code == HTTPStatus.OK
        assert mgr.has_credentials is True
        mgr.rediscover.assert_not_called()


def test_put_kasa_credentials_reload_failure_sets_restart_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Credentials stay saved when rediscover fails; response is not 500."""
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    mgr.rediscover = AsyncMock(side_effect=RuntimeError("lan down"))  # type: ignore[method-assign]
    state = DeviceManagersState(
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(discovery_cache=str(db)),
    )
    with (
        runtime.temporary_device_state(state),
        patch.object(runtime, "restart_device_state_watchers", new_callable=AsyncMock) as restart_mock,
    ):
        r = client.put(
            "/v1/settings/kasa-credentials",
            json={"username": "alice@example.com", "password": "hunter2"},
        )
        assert r.status_code == HTTPStatus.OK
        assert r.json()["configured"] is True
        assert r.json()["restart_required"] is True
        assert kasa_credentials_stored_in_db(db) is True
        restart_mock.assert_not_called()


def test_put_kasa_credentials_hot_reloads_manager(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    mgr.rediscover = AsyncMock()  # type: ignore[method-assign]
    state = DeviceManagersState(
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(discovery_cache=str(db)),
    )
    with (
        runtime.temporary_device_state(state),
        patch.object(runtime, "restart_device_state_watchers", new_callable=AsyncMock) as restart_mock,
    ):
        r = client.put(
            "/v1/settings/kasa-credentials",
            json={"username": "alice@example.com", "password": "hunter2"},
        )
        assert r.status_code == HTTPStatus.OK
        assert r.json()["restart_required"] is False
        assert mgr.has_credentials is True
        mgr.rediscover.assert_awaited_once()
        restart_mock.assert_awaited_once()
