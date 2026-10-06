"""Tests for Vizio TV settings routes."""

from __future__ import annotations

import argparse
import logging
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.db.secrets import SecretsConfigurationError, SecretsDecryptError, save_vizio_auth_token_to_db
from app.domesti_bot_cli import DeviceManagersState
from app.server_runtime import runtime as server_runtime
from app.vizio_credentials import resolve_vizio_auth_token
from app.vizio_smartcast_client import VizioDeviceInfoSnapshot


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        vizio_auth_token=None,
        vizio_host=[],
        no_vizio=False,
    )
    app = create_app(args)
    return TestClient(app), app


def test_get_vizio_tvs_reports_secrets_key_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.get("/v1/settings/vizio/tvs")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["secrets_key_configured"] is True
    assert body["secrets_key_source"] == "env"
    assert body["tvs"] == []


def test_put_vizio_auth_persists_token_and_tv_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    info = VizioDeviceInfoSnapshot(
        model_name="V505M-K09",
        cast_name="Kitchen TV",
        diid="abc",
        mac="00:bd:3e:d5:f0:11",
    )
    with (
        patch(
            "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
            new_callable=AsyncMock,
            return_value=info,
        ),
        patch(
            "app.api.vizio_settings_routes.resolve_vizio_tv_mac",
            new_callable=AsyncMock,
            return_value="00:bd:3e:d5:f0:11",
        ),
    ):
        response = client.put(
            "/v1/settings/vizio/tvs/192.168.86.201/auth",
            json={"token": "Zmowtcpoxo"},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["configured"] is True
    assert body["device_id"] == "00:bd:3e:d5:f0:11"

    rows = device_discovery_store.load_vizio_tvs(db)
    assert rows[0][4] == "00:bd:3e:d5:f0:11"

    listed = client.get("/v1/settings/vizio/tvs").json()
    assert len(listed["tvs"]) == 1
    assert listed["tvs"][0]["device_id"] == "00:bd:3e:d5:f0:11"
    assert listed["tvs"][0]["mac"] == "00:bd:3e:d5:f0:11"
    assert listed["tvs"][0]["display_name"] == "Kitchen TV"
    assert listed["tvs"][0]["auth_configured"] is True
    assert listed["tvs"][0]["auth_source"] == "database"
    assert "stored_token" not in listed["tvs"][0]
    assert "Zmowtcpoxo" not in str(listed)
    assert isinstance(listed["tvs"][0]["updated_at"], float)


def test_put_vizio_auth_without_cache_returns_409(tmp_path: Path) -> None:
    client, _app = _client(cache_path=None)
    response = client.put(
        "/v1/settings/vizio/tvs/192.168.86.201/auth",
        json={"token": "secret"},
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert "discovery cache" in response.json()["detail"].lower()


def test_delete_vizio_auth_clears_database_row(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with (
        patch(
            "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.api.vizio_settings_routes.resolve_vizio_tv_mac",
            new_callable=AsyncMock,
            return_value="00:bd:3e:d5:f0:11",
        ),
    ):
        put = client.put(
            "/v1/settings/vizio/tvs/192.168.86.201/auth",
            json={"token": "secret"},
        )
    assert put.status_code == HTTPStatus.OK

    deleted = client.delete("/v1/settings/vizio/auth/00:bd:3e:d5:f0:11")
    assert deleted.status_code == HTTPStatus.OK
    assert deleted.json()["device_id"] == "00:bd:3e:d5:f0:11"
    assert deleted.json()["auth_configured"] is False
    assert "stored_token" not in deleted.json()
    assert deleted.json()["updated_at"] is None


def test_put_vizio_auth_hot_reloads_manager(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    info = VizioDeviceInfoSnapshot(
        model_name="V505M-K09",
        cast_name="Kitchen TV",
        diid="abc",
        mac="00:bd:3e:d5:f0:11",
    )
    mock_tv = object()
    mock_mgr = AsyncMock()
    mock_mgr.tvs = (mock_tv,)
    mock_mgr.disconnect = AsyncMock()
    mock_mgr.fetch = AsyncMock()
    args = argparse.Namespace(
        discovery_cache=str(db),
        vizio_auth_token=None,
        vizio_host=[],
        no_vizio=False,
    )
    monkeypatch.setattr(
        server_runtime,
        "device_state",
        DeviceManagersState(
            kasa_mgr=MagicMock(),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=args,
        ),
    )
    with (
        patch(
            "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
            new_callable=AsyncMock,
            return_value=info,
        ),
        patch(
            "app.api.vizio_settings_routes.VizioDeviceManager",
        ) as mgr_cls,
        patch(
            "app.api.vizio_settings_routes.runtime.restart_device_state_watchers",
            new_callable=AsyncMock,
        ) as restart_watchers,
    ):
        mgr_cls.return_value = mock_mgr
        response = client.put(
            "/v1/settings/vizio/tvs/192.168.86.201/auth",
            json={"token": "secret"},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["restart_required"] is False
    mock_mgr.fetch.assert_awaited_once()
    restart_watchers.assert_awaited_once()
    mgr_cls.assert_called_once()
    assert mgr_cls.call_args.kwargs["force_discovery"] is False
    assert server_runtime.device_state is not None
    assert server_runtime.device_state.vizio_mgr is mock_mgr


def test_resolve_vizio_auth_falls_back_to_env_when_db_decrypt_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIZIO_AUTH_TOKEN", "env-token")
    with patch(
        "app.vizio_credentials.load_vizio_auth_token_from_db",
        side_effect=SecretsDecryptError("bad ciphertext"),
    ):
        token, source = resolve_vizio_auth_token(
            mac=None,
            host="192.168.86.201",
            cli_token=None,
            env_token="env-token",
            cache_path=tmp_path / "ui.sqlite",
        )
    assert token == "env-token"
    assert source == "env"


def test_resolve_vizio_auth_falls_back_to_env_when_secrets_key_is_invalid(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("VIZIO_AUTH_TOKEN", "env-token")
    with patch(
        "app.vizio_credentials.load_vizio_auth_token_from_db",
        side_effect=SecretsConfigurationError("malformed Fernet key"),
    ):
        token, source = resolve_vizio_auth_token(
            mac="00:bd:3e:d5:f0:11",
            host="192.168.86.201",
            cli_token=None,
            env_token="env-token",
            cache_path=tmp_path / "ui.sqlite",
        )
    assert token == "env-token"
    assert source == "env"


def _remember_tv(db: Path) -> None:
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid="abc",
    )


def test_vizio_tv_list_with_an_undecryptable_row_is_stored_but_not_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid="abc",
    )
    save_vizio_auth_token_to_db(db, token="Zmowtcpoxo", mac="00:bd:3e:d5:f0:11")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=db)
    response = client.get("/v1/settings/vizio/tvs")
    assert response.status_code == HTTPStatus.OK
    tv = response.json()["tvs"][0]
    assert tv["auth_configured"] is False
    assert tv["auth_source"] == "none"
    assert isinstance(tv["updated_at"], float)


def test_vizio_tv_list_database_row_wins_over_env_and_neither_token_is_returned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid="abc",
    )
    save_vizio_auth_token_to_db(db, token="Zmowtcpoxo", host="192.168.86.201")
    monkeypatch.setenv("VIZIO_AUTH_TOKEN", "env-token-value")
    client, _app = _client(cache_path=db)
    tv = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]
    assert tv["auth_source"] == "database"
    assert isinstance(tv["updated_at"], float)
    assert "Zmowtcpoxo" not in str(tv)
    assert "env-token-value" not in str(tv)


def test_vizio_tv_list_with_only_an_env_token_has_no_updated_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    _remember_tv(db)
    monkeypatch.setenv("VIZIO_AUTH_TOKEN", "env-token-value")
    client, _app = _client(cache_path=db)
    tv = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]
    assert tv["auth_source"] == "env"
    assert tv["updated_at"] is None
    assert "env-token-value" not in str(tv)


def test_vizio_tv_list_updated_at_prefers_the_mac_row_over_a_legacy_host_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    _remember_tv(db)
    times = iter([100.0, 200.0])
    monkeypatch.setattr("app.db.secrets.time", SimpleNamespace(time=lambda: next(times)))
    save_vizio_auth_token_to_db(db, token="host-token-aaaa", host="192.168.86.201")
    save_vizio_auth_token_to_db(db, token="mac-token-bbbb", mac="00:bd:3e:d5:f0:11")
    # save_vizio_auth_token_to_db with only a MAC leaves the host row, so both exist here.
    client, _app = _client(cache_path=db)
    tv = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]
    assert tv["updated_at"] == 200.0


def test_vizio_tv_list_updated_at_changes_when_the_token_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    _remember_tv(db)
    client, _app = _client(cache_path=db)
    times = iter([100.0, 300.0])
    monkeypatch.setattr("app.db.secrets.time", SimpleNamespace(time=lambda: next(times)))
    save_vizio_auth_token_to_db(db, token="first-token-aaaa", mac="00:bd:3e:d5:f0:11")
    first = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]["updated_at"]
    save_vizio_auth_token_to_db(db, token="second-token-bbbb", mac="00:bd:3e:d5:f0:11")
    second = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]["updated_at"]
    assert (first, second) == (100.0, 300.0)


def test_vizio_tv_list_cli_token_still_reports_the_stored_row(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    _remember_tv(db)
    save_vizio_auth_token_to_db(db, token="stored-token-aaaa", mac="00:bd:3e:d5:f0:11")
    args = argparse.Namespace(
        discovery_cache=str(db),
        vizio_auth_token="cli-token-value",
        vizio_host=[],
        no_vizio=False,
    )
    client = TestClient(create_app(args))
    tv = client.get("/v1/settings/vizio/tvs").json()["tvs"][0]
    assert tv["auth_source"] == "cli"
    assert isinstance(tv["updated_at"], float)
    assert "cli-token-value" not in str(tv)
    assert "stored-token-aaaa" not in str(tv)


def test_vizio_resolver_logs_why_an_unreadable_token_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_vizio_auth_token_to_db(db, token="stored-token-aaaa", mac="00:bd:3e:d5:f0:11")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.vizio_credentials"):
        token, source = resolve_vizio_auth_token(
            mac="00:bd:3e:d5:f0:11",
            host="192.168.86.201",
            cli_token=None,
            env_token=None,
            cache_path=db,
        )
    assert (token, source) == ("", "none")
    assert "cannot be read" in caplog.text
    assert "SecretsDecryptError" in caplog.text
    assert "stored-token-aaaa" not in caplog.text
