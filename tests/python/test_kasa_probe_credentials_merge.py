"""A partial Kasa Test override is completed from the stored pair, because the password is write-only."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db.secrets import save_kasa_credentials_to_db
from app.device_enums import SettingsCredentialsTestSource
from app.kasa_credentials import resolve_kasa_credentials
from app.settings_credentials_test import CredentialsTestUnavailableError, _resolve_kasa_probe_credentials


@pytest.fixture
def stored_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("KASA_USERNAME", raising=False)
    monkeypatch.delenv("KASA_PASSWORD", raising=False)
    db = tmp_path / "ui.sqlite"
    save_kasa_credentials_to_db(db, username="alice@example.com", password="hunter2")
    return db


def test_changed_email_with_blank_password_uses_the_stored_password(stored_db: Path) -> None:
    creds, source = _resolve_kasa_probe_credentials(cache_path=stored_db, username="bob@example.com", password=None)
    assert (creds.username, creds.password) == ("bob@example.com", "hunter2")
    assert source is SettingsCredentialsTestSource.FORM


def test_new_password_with_blank_email_uses_the_stored_email(stored_db: Path) -> None:
    creds, source = _resolve_kasa_probe_credentials(cache_path=stored_db, username="", password="new-password-1")
    assert (creds.username, creds.password) == ("alice@example.com", "new-password-1")
    assert source is SettingsCredentialsTestSource.FORM


def test_a_partial_override_with_nothing_stored_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(CredentialsTestUnavailableError):
        _resolve_kasa_probe_credentials(cache_path=tmp_path / "ui.sqlite", username="bob@example.com", password=None)


def test_a_full_override_wins_over_the_stored_pair(stored_db: Path) -> None:
    creds, source = _resolve_kasa_probe_credentials(
        cache_path=stored_db, username="bob@example.com", password="other-password"
    )
    assert (creds.username, creds.password) == ("bob@example.com", "other-password")
    assert source is SettingsCredentialsTestSource.FORM


def test_environment_credentials_are_not_mixed_with_a_single_form_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("KASA_USERNAME", "env@example.com")
    monkeypatch.setenv("KASA_PASSWORD", "env-password")
    with pytest.raises(CredentialsTestUnavailableError, match="environment credentials are not mixed"):
        _resolve_kasa_probe_credentials(cache_path=tmp_path / "ui.sqlite", username=None, password="typed-password")


def test_kasa_resolver_ignores_an_undecryptable_row_and_logs_why(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stored_db: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    with caplog.at_level(logging.WARNING, logger="app.kasa_credentials"):
        assert resolve_kasa_credentials(cache_path=stored_db) == (None, "none")
    assert "cannot be decrypted" in caplog.text
    assert "hunter2" not in caplog.text
    assert "alice@example.com" not in caplog.text
