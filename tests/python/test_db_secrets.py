"""Tests for encrypted application secrets."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    delete_app_secret,
    load_tailwind_token_from_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    tailwind_token_stored_in_db,
)
from app.db.secrets_key import (
    generate_fernet_key,
    load_secrets_key_material,
    secrets_json_path,
    write_secrets_json,
)
from app.tailwind_credentials import resolve_tailwind_token


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DOMESTI_SECRETS_KEY", key)
    return key


def test_secrets_key_configured_when_env_valid(fernet_key: str) -> None:
    assert secrets_key_configured() is True
    assert secrets_key_source() == "env"


def test_secrets_key_from_json_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = Fernet.generate_key().decode("ascii")
    secrets_file = tmp_path / "domesti-secrets.json"
    secrets_file.write_text(
        json.dumps({"domesti_secrets_key": key}), encoding="utf-8"
    )
    monkeypatch.delenv("DOMESTI_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_SECRETS_FILE", str(secrets_file))
    material, source = load_secrets_key_material()
    assert material == key
    assert source == "file"
    assert secrets_key_configured() is True
    assert secrets_key_source() == "file"


def test_write_secrets_json_sets_mode_600(tmp_path: Path) -> None:
    key = generate_fernet_key()
    target = tmp_path / "domesti-secrets.json"
    written = write_secrets_json(key, path=target)
    assert written == target
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["domesti_secrets_key"] == key


def test_save_and_load_tailwind_token_roundtrip(
    tmp_path: Path, fernet_key: str
) -> None:
    db = tmp_path / "secrets.sqlite"
    save_tailwind_token_to_db(db, "123456")
    assert load_tailwind_token_from_db(db) == "123456"
    assert tailwind_token_stored_in_db(db) is True


def test_delete_app_secret_clears_tailwind_token(
    tmp_path: Path, fernet_key: str
) -> None:
    db = tmp_path / "secrets.sqlite"
    save_tailwind_token_to_db(db, "123456")
    delete_app_secret(db, key="tailwind_token")
    assert load_tailwind_token_from_db(db) is None
    assert tailwind_token_stored_in_db(db) is False


def test_save_without_secrets_key_raises(tmp_path: Path) -> None:
    db = tmp_path / "secrets.sqlite"
    with pytest.raises(SecretsConfigurationError):
        save_tailwind_token_to_db(db, "123456")


def test_load_with_wrong_key_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "secrets.sqlite"
    monkeypatch.setenv("DOMESTI_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    save_tailwind_token_to_db(db, "123456")
    monkeypatch.setenv("DOMESTI_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    with pytest.raises(SecretsDecryptError):
        load_tailwind_token_from_db(db)


def test_resolve_tailwind_token_precedence(
    tmp_path: Path, fernet_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "secrets.sqlite"
    save_tailwind_token_to_db(db, "111111")
    token, source = resolve_tailwind_token(cli_token=None, cache_path=db)
    assert token == "111111"
    assert source == "database"

    monkeypatch.setenv("TAILWIND_TOKEN", "222222")
    token, source = resolve_tailwind_token(cli_token=None, cache_path=db)
    assert token == "222222"
    assert source == "env"

    token, source = resolve_tailwind_token(cli_token="333333", cache_path=db)
    assert token == "333333"
    assert source == "cli"
