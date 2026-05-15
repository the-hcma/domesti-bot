"""Tests for Tailwind token settings routes."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.secrets import save_tailwind_token_to_db


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def test_get_tailwind_token_settings_reports_env_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("TAILWIND_TOKEN", "654321")
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/tailwind-token")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "env"
    assert body["stored_in_database"] is False


def test_put_tailwind_token_persists_when_secrets_key_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    monkeypatch.setenv("DOMESTI_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    r = client.put("/v1/settings/tailwind-token", json={"token": "123456"})
    assert r.status_code == HTTPStatus.OK
    get_r = client.get("/v1/settings/tailwind-token")
    assert get_r.json()["secrets_key_source"] == "env"


def test_put_tailwind_token_persists_when_secrets_key_in_json_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    monkeypatch.delenv("DOMESTI_SECRETS_KEY", raising=False)
    key = Fernet.generate_key().decode("ascii")
    secrets_file = tmp_path / "domesti-secrets.json"
    secrets_file.write_text(json.dumps({"domesti_secrets_key": key}), encoding="utf-8")
    monkeypatch.setenv("DOMESTI_SECRETS_FILE", str(secrets_file))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    r = client.put("/v1/settings/tailwind-token", json={"token": "123456"})
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "database"
    assert body["restart_required"] is True

    get_r = client.get("/v1/settings/tailwind-token")
    body = get_r.json()
    assert body["stored_in_database"] is True
    assert body["stored_token"] == "123456"
    assert body["secrets_key_source"] == "file"


def test_put_tailwind_token_without_secrets_key_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DOMESTI_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_SECRETS_FILE", str(tmp_path / "missing-secrets.json"))
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.put("/v1/settings/tailwind-token", json={"token": "123456"})
    assert r.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert "domesti-secrets.json" in r.json()["detail"]


def test_put_tailwind_token_without_cache_returns_409(tmp_path: Path) -> None:
    client, _app = _client(cache_path=None)
    r = client.put("/v1/settings/tailwind-token", json={"token": "123456"})
    assert r.status_code == HTTPStatus.CONFLICT
    assert "discovery cache" in r.json()["detail"].lower()


def test_delete_tailwind_token_clears_database_row(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("TAILWIND_TOKEN", raising=False)
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DOMESTI_SECRETS_KEY", key)
    db = tmp_path / "ui.sqlite"
    save_tailwind_token_to_db(db, "123456")
    client, _app = _client(cache_path=db)
    r = client.delete("/v1/settings/tailwind-token")
    assert r.status_code == HTTPStatus.OK
    assert r.json()["configured"] is False
    assert r.json()["stored_in_database"] is False
