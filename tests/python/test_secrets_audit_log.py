"""Secret writes are audited by key and action, never by value."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db.secrets import (
    delete_app_secret,
    delete_kasa_credentials_from_db,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    save_vizio_auth_token_to_db,
)

_VALUE = "-".join(["SENTINEL", "audit", "value"])


@pytest.fixture(autouse=True)
def _fernet_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))


def _audit_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "app.db.secrets"]


def test_first_save_is_logged_as_created_and_a_second_as_replaced(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        save_tailwind_token_to_db(db, _VALUE)
        save_tailwind_token_to_db(db, _VALUE + "2")
    assert _audit_messages(caplog) == [
        "secret created key=tailwind_token",
        "secret replaced key=tailwind_token",
    ]


def test_delete_is_logged_once_and_a_noop_delete_is_silent(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    save_tailwind_token_to_db(db, _VALUE)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        delete_app_secret(db, key="tailwind_token")
        delete_app_secret(db, key="tailwind_token")
    assert _audit_messages(caplog) == ["secret removed key=tailwind_token"]


def test_kasa_save_and_delete_log_both_rows(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        save_kasa_credentials_to_db(db, username="alice@example.com", password=_VALUE)
        delete_kasa_credentials_from_db(db)
    messages = _audit_messages(caplog)
    assert "secret created key=kasa_password" in messages
    assert "secret created key=kasa_username" in messages
    assert "secret removed key=kasa_password" in messages
    assert "secret removed key=kasa_username" in messages


def test_audit_log_never_contains_the_secret_value(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.DEBUG):
        save_tailwind_token_to_db(db, _VALUE)
        save_kasa_credentials_to_db(db, username="alice@example.com", password=_VALUE)
        delete_app_secret(db, key="tailwind_token")
    assert any("secret created" in message for message in _audit_messages(caplog))
    assert _VALUE not in caplog.text
    assert "alice@example.com" not in caplog.text


def test_a_failed_write_logs_nothing(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _boom(_path: Path, _write: object) -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr("app.db.secrets.discovery_write", _boom)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(RuntimeError):
        save_tailwind_token_to_db(tmp_path / "ui.sqlite", _VALUE)
    assert _audit_messages(caplog) == []


def test_vizio_token_keys_are_logged_by_mac(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        save_vizio_auth_token_to_db(db, token=_VALUE, mac="00:bd:3e:d5:f0:11")
    assert _audit_messages(caplog) == ["secret created key=vizio_auth:00:bd:3e:d5:f0:11"]
