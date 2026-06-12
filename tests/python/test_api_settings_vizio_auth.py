"""Tests for Vizio TV settings routes."""

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


def test_get_vizio_tvs_reports_secrets_key_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.get("/v1/settings/vizio/tvs")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["secrets_key_configured"] is True
    assert body["secrets_key_source"] == "env"
    assert body["tvs"] == []


def test_put_vizio_auth_persists_token_and_tv_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("VIZIO_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    info = VizioDeviceInfoSnapshot(
        model_name="V505M-K09",
        cast_name="Kitchen TV",
        diid="abc",
    )
    with patch(
        "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
        new_callable=AsyncMock,
        return_value=info,
    ):
        response = client.put(
            "/v1/settings/vizio/tvs/192.168.86.201/auth",
            json={"token": "Zmowtcpoxo"},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["configured"] is True
    assert body["device_id"] == "192.168.86.201"

    listed = client.get("/v1/settings/vizio/tvs").json()
    assert len(listed["tvs"]) == 1
    assert listed["tvs"][0]["display_name"] == "Kitchen TV"
    assert listed["tvs"][0]["auth_configured"] is True
    assert listed["tvs"][0]["auth_source"] == "database"


def test_put_vizio_auth_without_cache_returns_409(tmp_path: Path) -> None:
    client, _app = _client(cache_path=None)
    response = client.put(
        "/v1/settings/vizio/tvs/192.168.86.201/auth",
        json={"token": "secret"},
    )
    assert response.status_code == HTTPStatus.CONFLICT
    assert "discovery cache" in response.json()["detail"].lower()


def test_delete_vizio_auth_clears_database_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch(
        "app.api.vizio_settings_routes.VizioSmartCastClient.fetch_deviceinfo",
        new_callable=AsyncMock,
        return_value=None,
    ):
        put = client.put(
            "/v1/settings/vizio/tvs/192.168.86.201/auth",
            json={"token": "secret"},
        )
    assert put.status_code == HTTPStatus.OK

    deleted = client.delete("/v1/settings/vizio/auth/192.168.86.201:7345")
    assert deleted.status_code == HTTPStatus.OK
    assert deleted.json()["device_id"] == "192.168.86.201"
    assert deleted.json()["auth_configured"] is False
