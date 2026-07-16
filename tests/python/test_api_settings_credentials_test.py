"""Tests for Settings credential Test endpoints."""

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
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    save_vizio_auth_token_to_db,
)
from app.device_enums import SettingsCredentialsTestSource
from app.mytracks_service import MyTracksSyncError
from app.mytracks_store import MyTracksConfigSave, save_mytracks_config
from app.settings_credentials_test import CredentialsTestResult
from app.vizio_smartcast_client import VizioSmartCastAuthError, VizioSmartCastBusyError


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
        vizio_auth_token=None,
        vizio_host=[],
        no_vizio=False,
    )
    app = create_app(args)
    return TestClient(app), app


def test_post_tailwind_token_test_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_tailwind_token_to_db(db, "123456")
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.settings_routes.probe_tailwind_token",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=True,
            detail="Tailwind token ok at 192.168.1.10 (2 door(s))",
            source=SettingsCredentialsTestSource.DATABASE,
        ),
    ) as probe:
        response = client.post("/v1/settings/tailwind-token/test", json={})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert "door" in body["detail"]
    assert body["source"] == "database"
    probe.assert_awaited_once()


def test_post_tailwind_token_test_auth_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TAILWIND_TOKEN", "654321")
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    with patch(
        "app.api.settings_routes.probe_tailwind_token",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=False,
            detail="Tailwind token probe failed at 192.168.1.10: unauthorized",
            source=SettingsCredentialsTestSource.ENV,
        ),
    ):
        response = client.post(
            "/v1/settings/tailwind-token/test",
            json={"token": "000000", "host": "192.168.1.10"},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is False
    assert "unauthorized" in body["detail"]


def test_post_tailwind_token_test_not_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post("/v1/settings/tailwind-token/test", json={})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "No Tailwind token" in response.json()["detail"]


def test_post_kasa_credentials_test_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_kasa_credentials_to_db(db, username="alice@example.com", password="hunter2")
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.settings_routes.probe_kasa_credentials",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=True,
            detail="KLAP auth ok on 1 host(s): 192.168.1.20=on",
            source=SettingsCredentialsTestSource.DATABASE,
        ),
    ):
        response = client.post("/v1/settings/kasa-credentials/test", json={})
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "database"


def test_post_kasa_credentials_test_auth_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    with patch(
        "app.api.settings_routes.probe_kasa_credentials",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=False,
            detail="KLAP authentication failed: 192.168.1.20: bad password",
            source=SettingsCredentialsTestSource.FORM,
        ),
    ):
        response = client.post(
            "/v1/settings/kasa-credentials/test",
            json={"username": "alice@example.com", "password": "wrong"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False


def test_post_kasa_credentials_test_not_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post("/v1/settings/kasa-credentials/test", json={})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "No Kasa credentials" in response.json()["detail"]


def test_post_kasa_credentials_test_no_klap_hosts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post("/v1/settings/kasa-credentials/test", json={})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "No known KLAP hosts" in response.json()["detail"]


@pytest.mark.asyncio
async def test_probe_kasa_credentials_auth_failure_when_connect_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Connect exhaustion on a KLAP host must count as auth failure, not unreachable."""

    from kasa.credentials import Credentials
    from kasa.exceptions import AuthenticationError

    from app.settings_credentials_test import probe_kasa_credentials

    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    klap_cfg = {
        "host": "192.168.1.20",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    with (
        patch(
            "app.settings_credentials_test._klap_hosts_for_probe",
            return_value=[("192.168.1.20", klap_cfg)],
        ),
        patch(
            "app.settings_credentials_test._connect_from_saved_config",
            new_callable=AsyncMock,
            side_effect=AuthenticationError("KLAP authentication failed for 192.168.1.20"),
        ),
        patch(
            "app.settings_credentials_test._resolve_kasa_probe_credentials",
            return_value=(
                Credentials(username="alice@example.com", password="hunter2"),
                SettingsCredentialsTestSource.ENV,
            ),
        ),
    ):
        result = await probe_kasa_credentials(cache_path=tmp_path / "ui.sqlite")

    assert result.ok is False
    assert "KLAP authentication failed" in result.detail
    assert "192.168.1.20" in result.detail


@pytest.mark.asyncio
async def test_probe_kasa_credentials_unreachable_when_connect_returns_none(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Network-style connect exhaustion must not be labeled as KLAP auth failure."""

    from kasa.credentials import Credentials
    from kasa.exceptions import _ConnectionError

    from app.settings_credentials_test import probe_kasa_credentials

    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    klap_cfg = {
        "host": "192.168.1.20",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    with (
        patch(
            "app.settings_credentials_test._klap_hosts_for_probe",
            return_value=[("192.168.1.20", klap_cfg)],
        ),
        patch(
            "app.settings_credentials_test._connect_from_saved_config",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "app.settings_credentials_test._resolve_kasa_probe_credentials",
            return_value=(
                Credentials(username="alice@example.com", password="hunter2"),
                SettingsCredentialsTestSource.ENV,
            ),
        ),
    ):
        result = await probe_kasa_credentials(cache_path=tmp_path / "ui.sqlite")

    assert result.ok is False
    assert "Could not reach KLAP hosts" in result.detail
    assert "authentication failed" not in result.detail.lower()


@pytest.mark.asyncio
async def test_probe_kasa_credentials_auth_failure_after_plain_http_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Auth failure on the plain-HTTP retry must count as KLAP auth failure."""

    from kasa.credentials import Credentials
    from kasa.exceptions import AuthenticationError, _ConnectionError

    from app.settings_credentials_test import probe_kasa_credentials

    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    klap_cfg = {
        "host": "192.168.1.20",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }

    async def _connect_saved(cfg, *, credentials, timeout, raise_auth_failure=False):
        del cfg, credentials, timeout
        if raise_auth_failure:
            raise AuthenticationError("KLAP authentication failed for 192.168.1.20")
        return None

    with (
        patch(
            "app.settings_credentials_test._klap_hosts_for_probe",
            return_value=[("192.168.1.20", klap_cfg)],
        ),
        patch(
            "app.settings_credentials_test._connect_from_saved_config",
            side_effect=_connect_saved,
        ),
        patch(
            "app.settings_credentials_test._resolve_kasa_probe_credentials",
            return_value=(
                Credentials(username="alice@example.com", password="hunter2"),
                SettingsCredentialsTestSource.ENV,
            ),
        ),
    ):
        result = await probe_kasa_credentials(cache_path=tmp_path / "ui.sqlite")

    assert result.ok is False
    assert "KLAP authentication failed" in result.detail
    assert "192.168.1.20" in result.detail


@pytest.mark.asyncio
async def test_probe_kasa_credentials_missing_profile_propagates_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kasa.credentials import Credentials

    from app.settings_credentials_test import (
        CredentialsTestUnavailableError,
        probe_kasa_credentials,
    )

    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    with (
        patch(
            "app.settings_credentials_test._klap_hosts_for_probe",
            return_value=[("192.168.1.20", None)],
        ),
        patch(
            "app.settings_credentials_test._resolve_kasa_probe_credentials",
            return_value=(
                Credentials(username="alice@example.com", password="hunter2"),
                SettingsCredentialsTestSource.ENV,
            ),
        ),
    ):
        with pytest.raises(CredentialsTestUnavailableError, match="discovery first"):
            await probe_kasa_credentials(cache_path=tmp_path / "ui.sqlite")


@pytest.mark.asyncio
async def test_probe_kasa_credentials_partial_when_one_host_lacks_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from kasa.credentials import Credentials

    from app.settings_credentials_test import probe_kasa_credentials

    monkeypatch.setenv("KASA_USERNAME", "alice@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "hunter2")
    klap_cfg = {
        "host": "192.168.1.10",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    with (
        patch(
            "app.settings_credentials_test._klap_hosts_for_probe",
            return_value=[
                ("192.168.1.10", klap_cfg),
                ("192.168.1.20", None),
            ],
        ),
        patch(
            "app.settings_credentials_test._probe_one_kasa_host",
            new_callable=AsyncMock,
            return_value="on",
        ),
        patch(
            "app.settings_credentials_test._resolve_kasa_probe_credentials",
            return_value=(
                Credentials(username="alice@example.com", password="hunter2"),
                SettingsCredentialsTestSource.ENV,
            ),
        ),
    ):
        result = await probe_kasa_credentials(cache_path=tmp_path / "ui.sqlite")

    assert result.ok is False
    assert "192.168.1.10=on" in result.detail
    assert "192.168.1.20" in result.detail
    assert "no cached KLAP connection profile" in result.detail


def test_post_mytracks_credentials_test_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_mytracks_config(
        db,
        MyTracksConfigSave(domain="tracks.example.com", username="admin"),
    )
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.mytracks_routes.probe_mytracks_credentials",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=True,
            detail="My Tracks credentials ok (2 user(s))",
            source=SettingsCredentialsTestSource.FORM,
        ),
    ):
        response = client.post(
            "/v1/settings/my-tracks/test",
            json={"password": "secret"},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert "user" in body["detail"]


def test_post_mytracks_credentials_test_auth_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_mytracks_config(
        db,
        MyTracksConfigSave(domain="tracks.example.com", username="admin"),
    )
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.mytracks_routes.probe_mytracks_credentials",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=False,
            detail="My Tracks rejected the admin username or password",
            source=SettingsCredentialsTestSource.FORM,
        ),
    ):
        response = client.post(
            "/v1/settings/my-tracks/test",
            json={"password": "wrong"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False


def test_post_mytracks_credentials_test_missing_password(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post("/v1/settings/my-tracks/test", json={})
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_post_mytracks_credentials_test_no_domain(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post(
        "/v1/settings/my-tracks/test",
        json={"password": "secret"},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "domain" in response.json()["detail"].lower()


def test_post_mytracks_probe_raises_sync_error_as_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_mytracks_config(
        db,
        MyTracksConfigSave(domain="tracks.example.com", username="admin"),
    )
    client, _app = _client(cache_path=db)
    with patch(
        "app.settings_credentials_test.fetch_users_from_my_tracks",
        side_effect=MyTracksSyncError("My Tracks rejected the admin username or password"),
    ):
        response = client.post(
            "/v1/settings/my-tracks/test",
            json={"password": "wrong"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False


def test_post_vizio_auth_test_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    save_vizio_auth_token_to_db(
        db,
        mac="00:bd:3e:d5:f0:11",
        host="192.168.86.201",
        token="Zmowtcpoxo",
    )
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.vizio_settings_routes.probe_vizio_auth",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=True,
            detail="Vizio auth ok at 192.168.86.201 (power on)",
            source=SettingsCredentialsTestSource.DATABASE,
        ),
    ):
        response = client.post(
            "/v1/settings/vizio/tvs/192.168.86.201/auth/test",
            json={},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["source"] == "database"


def test_post_vizio_auth_test_auth_fail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    with patch(
        "app.api.vizio_settings_routes.probe_vizio_auth",
        new_callable=AsyncMock,
        return_value=CredentialsTestResult(
            ok=False,
            detail="invalid auth token",
            source=SettingsCredentialsTestSource.FORM,
        ),
    ):
        response = client.post(
            "/v1/settings/vizio/tvs/192.168.86.201/auth/test",
            json={"token": "bad-token"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False


def test_post_vizio_auth_test_not_configured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post(
        "/v1/settings/vizio/tvs/192.168.86.201/auth/test",
        json={},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "No Vizio auth token" in response.json()["detail"]


def test_post_vizio_auth_test_unknown_device_id(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.post(
        "/v1/settings/vizio/tvs/host:notaport/auth/test",
        json={"token": "any"},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_post_vizio_probe_auth_error_as_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")

    async def _raise_auth(*_args: object, **_kwargs: object) -> bool:
        raise VizioSmartCastAuthError("invalid auth token")

    with patch(
        "app.settings_credentials_test.VizioSmartCastClient.get_power_on",
        new=_raise_auth,
    ):
        response = client.post(
            "/v1/settings/vizio/tvs/192.168.86.201/auth/test",
            json={"token": "bad-token"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False


def test_post_vizio_probe_busy_error_as_ok_false(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")

    async def _raise_busy(*_args: object, **_kwargs: object) -> bool:
        raise VizioSmartCastBusyError("Operation blocked")

    with patch(
        "app.settings_credentials_test.VizioSmartCastClient.get_power_on",
        new=_raise_busy,
    ):
        response = client.post(
            "/v1/settings/vizio/tvs/192.168.86.201/auth/test",
            json={"token": "token"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is False
