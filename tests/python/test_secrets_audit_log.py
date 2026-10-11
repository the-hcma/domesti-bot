"""Secret writes are audited by key and action, never by value."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.orm import Session

from app.db import secrets as secrets_module
from app.db.secrets import (
    app_secret_stored,
    delete_app_secret,
    delete_kasa_credentials_from_db,
    rotate_app_secrets,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    save_vizio_auth_token_to_db,
    update_app_secrets,
)
from app.db.writer import discovery_write

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
        "secret creating key=tailwind_token",
        "secret created key=tailwind_token",
        "secret replacing key=tailwind_token",
        "secret replaced key=tailwind_token",
    ]


def test_delete_is_logged_once_and_a_noop_delete_is_silent(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    save_tailwind_token_to_db(db, _VALUE)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        delete_app_secret(db, key="tailwind_token")
        delete_app_secret(db, key="tailwind_token")
    assert _audit_messages(caplog) == ["secret removing key=tailwind_token", "secret removed key=tailwind_token"]


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


def test_a_write_that_fails_before_the_transaction_runs_logs_nothing(
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


@pytest.fixture
def failing_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the writer's ``session.commit()`` raise, so the transaction really does roll back."""

    def _commit(_self: Session) -> None:
        raise RuntimeError("disk full while committing")

    monkeypatch.setattr(Session, "commit", _commit)


@pytest.mark.usefixtures("failing_commit")
def test_a_commit_that_fails_logs_the_intent_then_the_rollback_and_never_the_success(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(RuntimeError):
        save_tailwind_token_to_db(db, _VALUE)
    records = [r for r in caplog.records if r.name == "app.db.secrets"]
    assert [r.getMessage() for r in records] == [
        "secret creating key=tailwind_token",
        "secret change rolled back action=created key=tailwind_token",
    ]
    assert [r.levelno for r in records] == [logging.INFO, logging.WARNING]


@pytest.mark.usefixtures("failing_commit")
def test_a_failed_commit_rolls_back_every_row_of_a_multi_row_write(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(RuntimeError):
        save_kasa_credentials_to_db(tmp_path / "ui.sqlite", username="alice@example.com", password=_VALUE)
    assert _audit_messages(caplog) == [
        "secret creating key=kasa_password",
        "secret creating key=kasa_username",
        "secret change rolled back action=created key=kasa_password",
        "secret change rolled back action=created key=kasa_username",
    ]


def test_a_failed_delete_and_a_failed_rotation_log_their_rollbacks(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    save_tailwind_token_to_db(db, _VALUE)
    save_kasa_credentials_to_db(db, username="alice@example.com", password=_VALUE)
    old_key = os.environ["DOMESTI_BOT_SECRETS_KEY"]
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", ",".join([Fernet.generate_key().decode("ascii"), old_key]))

    def _commit(_self: Session) -> None:
        raise RuntimeError("disk full while committing")

    monkeypatch.setattr(Session, "commit", _commit)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        with pytest.raises(RuntimeError):
            delete_app_secret(db, key="tailwind_token")
        with pytest.raises(RuntimeError):
            delete_kasa_credentials_from_db(db)
        with pytest.raises(RuntimeError):
            rotate_app_secrets(db)
    messages = _audit_messages(caplog)
    assert "secret change rolled back action=removed key=tailwind_token" in messages
    assert "secret change rolled back action=removed key=kasa_username" in messages
    assert "secret change rolled back action=re-encrypted key=tailwind_token" in messages
    assert not [m for m in messages if m.startswith(("secret removed", "secret re-encrypted"))]


@pytest.mark.usefixtures("failing_commit")
def test_a_failed_noop_delete_logs_nothing(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(RuntimeError):
        delete_app_secret(tmp_path / "ui.sqlite", key="tailwind_token")
    assert _audit_messages(caplog) == []


def test_an_interrupt_in_the_calling_thread_is_not_reported_as_a_rollback(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _interrupted(path: Path, write: Callable[[Session], None]) -> None:
        discovery_write(path, write)
        raise KeyboardInterrupt

    monkeypatch.setattr("app.db.secrets.discovery_write", _interrupted)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(KeyboardInterrupt):
        save_tailwind_token_to_db(tmp_path / "ui.sqlite", _VALUE)
    messages = _audit_messages(caplog)
    assert messages == ["secret creating key=tailwind_token"]


def test_the_pending_line_precedes_the_committed_row(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    seen_at_pending: list[bool] = []
    real_pending = secrets_module._audit_secret_pending

    def _spy(action: str, key: str) -> None:
        seen_at_pending.append(app_secret_stored(db, key))
        real_pending(action, key)

    monkeypatch.setattr(secrets_module, "_audit_secret_pending", _spy)
    save_tailwind_token_to_db(db, _VALUE)
    assert seen_at_pending == [False]
    assert app_secret_stored(db, "tailwind_token")


def test_a_rejected_update_logs_nothing(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    def _reject(_read: Callable[[str], str | None]) -> tuple[dict[str, str | None], None]:
        raise ValueError("Expected a valid update, got none")

    with caplog.at_level(logging.INFO, logger="app.db.secrets"), pytest.raises(ValueError):
        update_app_secrets(tmp_path / "ui.sqlite", _reject)
    assert _audit_messages(caplog) == []


def test_vizio_token_keys_are_logged_by_mac(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        save_vizio_auth_token_to_db(db, token=_VALUE, mac="00:bd:3e:d5:f0:11")
    assert _audit_messages(caplog) == [
        "secret creating key=vizio_auth:00:bd:3e:d5:f0:11",
        "secret created key=vizio_auth:00:bd:3e:d5:f0:11",
    ]
