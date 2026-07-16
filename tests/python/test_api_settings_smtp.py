"""Tests for SMTP settings routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.smtp_store import load_smtp_config


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def test_get_smtp_settings_returns_null_when_unconfigured(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.get("/v1/settings/smtp")
    assert response.status_code == HTTPStatus.OK
    assert response.json() is None


def test_put_smtp_settings_persists_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    payload = {
        "host": "localhost",
        "port": 25,
        "username": "",
        "password": "secret",
        "mail_domain": "hcma.info",
        "from_address": "domestibot-noreply@hcma.info",
    }
    response = client.put("/v1/settings/smtp", json=payload)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["host"] == "localhost"
    assert body["port"] == 25
    assert body["mail_domain"] == "hcma.info"
    assert body["password_configured"] is True

    get_response = client.get("/v1/settings/smtp")
    assert get_response.json()["from_address"] == "domestibot-noreply@hcma.info"

    saved = load_smtp_config(db)
    assert saved is not None
    assert saved.host == "localhost"


@patch("app.smtp_service._LoggingSMTP")
def test_post_smtp_test_email_sends_via_plain_smtp(
    smtp_cls: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    smtp_instance = MagicMock()
    smtp_instance.smtp_data_code = 250
    smtp_instance.smtp_data_response = "2.0.0 Ok: queued as TESTQID"
    smtp_instance.send_message.return_value = {}
    smtp_cls.return_value.__enter__.return_value = smtp_instance

    response = client.post(
        "/v1/settings/smtp/test",
        json={
            "host": "localhost",
            "port": 25,
            "username": "",
            "password": "",
            "mail_domain": "hcma.info",
            "from_address": "domestibot-noreply@hcma.info",
            "to_address": "ops@hcma.info",
        },
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    smtp_cls.assert_called_once_with("localhost", 25, timeout=10.0)
    smtp_instance.send_message.assert_called_once()
    message = smtp_instance.send_message.call_args[0][0]
    plain_part = message.get_body(preferencelist=("plain",))
    assert plain_part is not None
    plain = plain_part.get_content()
    assert isinstance(plain, str)
    assert "http://testserver/#/automations/mail" in plain
    assert "Instance: http://testserver" in plain
    assert message["Subject"] == "domesti-bot [test] SMTP configuration"


def test_put_smtp_without_secrets_key_returns_503(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing-config.json"))
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.put(
        "/v1/settings/smtp",
        json={
            "host": "localhost",
            "port": 25,
            "username": "",
            "password": "secret",
            "mail_domain": "hcma.info",
            "from_address": "domestibot-noreply@hcma.info",
        },
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_delete_smtp_settings_clears_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/settings/smtp",
        json={
            "host": "localhost",
            "port": 25,
            "username": "",
            "password": "secret",
            "mail_domain": "hcma.info",
            "from_address": "domestibot-noreply@hcma.info",
        },
    )
    delete_response = client.delete("/v1/settings/smtp")
    assert delete_response.status_code == HTTPStatus.NO_CONTENT
    assert client.get("/v1/settings/smtp").json() is None
