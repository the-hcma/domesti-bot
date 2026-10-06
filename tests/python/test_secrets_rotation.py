"""Fernet master key rotation: a key list (newest first) and an atomic re-encrypt of every stored secret."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select

from app.db.models import AppSecret
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    load_kasa_credentials_from_db,
    load_tailwind_token_from_db,
    rotate_app_secrets,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
)
from app.db.secrets_key import load_secrets_key_material, parse_secrets_key_list, write_secrets_json
from app.db.session import discovery_session

_TOKEN = "-".join(["SENTINEL", "rotation", "token"])


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


def _use_keys(monkeypatch: pytest.MonkeyPatch, *keys: str) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", ",".join(keys))


def _ciphertexts(db: Path) -> dict[str, tuple[bytes, float]]:
    with discovery_session(db) as session:
        return {row.key: (bytes(row.ciphertext), row.updated_at) for row in session.scalars(select(AppSecret))}


def _decrypts_with(key: str, ciphertext: bytes) -> bool:
    try:
        Fernet(key.encode("ascii")).decrypt(ciphertext)
    except Exception:
        return False
    return True


def test_key_list_parsing_drops_blanks_and_whitespace() -> None:
    assert parse_secrets_key_list(" a , ,b,, c ") == ["a", "b", "c"]
    assert parse_secrets_key_list("   ") == []


def test_secrets_stay_readable_after_a_new_key_is_added_in_front(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)

    _use_keys(monkeypatch, new, old)
    assert secrets_key_configured() is True
    assert secrets_key_source() == "env"
    assert load_tailwind_token_from_db(db) == _TOKEN


def test_new_writes_use_the_newest_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    _use_keys(monkeypatch, new, old)
    save_tailwind_token_to_db(db, _TOKEN)
    ciphertext, _updated = _ciphertexts(db)["tailwind_token"]
    assert _decrypts_with(new, ciphertext)
    assert not _decrypts_with(old, ciphertext)


def test_an_invalid_key_anywhere_in_the_list_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_keys(monkeypatch, _key(), "not-a-fernet-key")
    with pytest.raises(SecretsConfigurationError):
        secrets_key_configured()
    assert secrets_key_source() == "none"


def test_rotate_reencrypts_every_row_under_the_newest_key_and_keeps_updated_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)
    save_kasa_credentials_to_db(db, username="me@example.test", password="kasa-pass")
    before = _ciphertexts(db)

    _use_keys(monkeypatch, new, old)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        result = rotate_app_secrets(db)

    assert sorted(result.rotated) == sorted(before)
    assert result.already_current == []
    assert result.undecryptable == []
    after = _ciphertexts(db)
    for name, (ciphertext, updated_at) in after.items():
        assert _decrypts_with(new, ciphertext), name
        assert not _decrypts_with(old, ciphertext), name
        assert updated_at == before[name][1], name
    messages = [r.getMessage() for r in caplog.records if r.name == "app.db.secrets"]
    assert sorted(messages) == sorted(f"secret re-encrypted key={name}" for name in before)
    assert _TOKEN not in caplog.text

    # The old key can now be dropped without losing anything.
    _use_keys(monkeypatch, new)
    assert load_tailwind_token_from_db(db) == _TOKEN
    assert load_kasa_credentials_from_db(db) == ("me@example.test", "kasa-pass")


def test_rotate_is_idempotent_and_skips_rows_already_on_the_newest_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)
    _use_keys(monkeypatch, new, old)
    rotate_app_secrets(db)
    first = _ciphertexts(db)

    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        result = rotate_app_secrets(db)

    assert result.rotated == []
    assert result.already_current == ["tailwind_token"]
    assert _ciphertexts(db) == first
    assert not [r for r in caplog.records if r.name == "app.db.secrets"]


def test_rotate_with_an_undecryptable_row_changes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    old, new, stranger = _key(), _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)
    _use_keys(monkeypatch, stranger)
    save_kasa_credentials_to_db(db, username="me@example.test", password="kasa-pass")
    before = _ciphertexts(db)

    _use_keys(monkeypatch, new, old)
    with pytest.raises(SecretsDecryptError) as excinfo:
        rotate_app_secrets(db)

    assert "kasa_password" in str(excinfo.value)
    assert _TOKEN not in str(excinfo.value)
    assert _ciphertexts(db) == before
    assert load_tailwind_token_from_db(db) == _TOKEN


def test_rotate_can_skip_undecryptable_rows_and_still_rotate_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new, stranger = _key(), _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)
    _use_keys(monkeypatch, stranger)
    save_kasa_credentials_to_db(db, username="me@example.test", password="kasa-pass")
    stranded = _ciphertexts(db)["kasa_password"]

    _use_keys(monkeypatch, new, old)
    result = rotate_app_secrets(db, skip_undecryptable=True)

    assert result.rotated == ["tailwind_token"]
    assert result.undecryptable == ["kasa_password", "kasa_username"]
    after = _ciphertexts(db)
    assert after["kasa_password"] == stranded
    assert _decrypts_with(new, after["tailwind_token"][0])


def test_rotate_without_a_configured_key_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing.json"))
    with pytest.raises(SecretsConfigurationError):
        rotate_app_secrets(tmp_path / "ui.sqlite")


def test_config_file_accepts_a_key_list_in_either_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    config = tmp_path / "domesti-bot.config.json"
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(config))
    new, old = _key(), _key()

    write_secrets_json(f"{new}, {old}", path=config)
    assert json.loads(config.read_text())["domesti_secrets_key"] == f"{new},{old}"
    assert load_secrets_key_material() == (f"{new},{old}", "file")

    config.write_text(json.dumps({"domesti_secrets_key": [new, old]}))
    assert load_secrets_key_material() == (f"{new},{old}", "file")

    with pytest.raises(ValueError, match="Fernet key"):
        write_secrets_json(f"{new},nope", path=config)


def test_an_empty_key_list_in_the_config_file_is_a_configuration_error_not_silently_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    config = tmp_path / "domesti-bot.config.json"
    config.write_text(json.dumps({"domesti_secrets_key": []}))
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(config))
    with pytest.raises(SecretsConfigurationError, match="at least one key"):
        secrets_key_configured()


@pytest.mark.parametrize("entry", [123, None, ["nested"], {"k": "v"}, True])
def test_a_non_string_entry_in_the_config_file_key_list_is_a_configuration_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: object
) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    config = tmp_path / "domesti-bot.config.json"
    config.write_text(json.dumps({"domesti_secrets_key": [_key(), entry]}))
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(config))
    with pytest.raises(SecretsConfigurationError, match="only strings"):
        secrets_key_configured()


def test_dry_run_reports_undecryptable_rows_without_raising_or_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new, stranger = _key(), _key(), _key()
    _use_keys(monkeypatch, old)
    save_tailwind_token_to_db(db, _TOKEN)
    _use_keys(monkeypatch, stranger)
    save_kasa_credentials_to_db(db, username="me@example.test", password="kasa-pass")
    before = _ciphertexts(db)

    _use_keys(monkeypatch, new, old)
    with caplog.at_level(logging.INFO, logger="app.db.secrets"):
        result = rotate_app_secrets(db, dry_run=True)

    assert result.rotated == ["tailwind_token"]
    assert result.undecryptable == ["kasa_password", "kasa_username"]
    assert _ciphertexts(db) == before
    assert not [r for r in caplog.records if r.name == "app.db.secrets"]
