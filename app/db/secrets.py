"""Encrypted application secrets stored in the discovery database."""

from __future__ import annotations

import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.db.models import AppSecret
from app.db.secrets_key import SecretsKeySource, load_secrets_key_material
from app.db.session import discovery_session

_TAILWIND_SECRET_KEY = "tailwind_token"


class SecretsConfigurationError(ValueError):
    """Raised when no valid Fernet key is configured."""


class SecretsDecryptError(ValueError):
    """Raised when ciphertext cannot be decrypted with the configured key."""


def delete_app_secret(path: Path, *, key: str) -> None:
    """Remove one secret row if present."""
    with discovery_session(path) as session:
        row = session.get(AppSecret, key.strip())
        if row is not None:
            session.delete(row)


def load_tailwind_token_from_db(path: Path) -> str | None:
    """Return the decrypted Tailwind token from the database, or ``None``."""
    fernet = _fernet_from_config()
    if fernet is None:
        return None
    with discovery_session(path) as session:
        row = session.get(AppSecret, _TAILWIND_SECRET_KEY)
        if row is None:
            return None
        try:
            plain = fernet.decrypt(row.ciphertext)
        except InvalidToken as exc:
            raise SecretsDecryptError(
                "Expected valid Fernet ciphertext for tailwind_token, got undecryptable data"
            ) from exc
        token = plain.decode("utf-8").strip()
        return token if token else None


def save_tailwind_token_to_db(path: Path, token: str) -> None:
    """Encrypt and persist the Tailwind Local Control Key."""
    fernet = _require_fernet()
    ciphertext = fernet.encrypt(token.strip().encode("utf-8"))
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(AppSecret, _TAILWIND_SECRET_KEY)
        if row is None:
            session.add(
                AppSecret(
                    key=_TAILWIND_SECRET_KEY,
                    ciphertext=ciphertext,
                    updated_at=now,
                )
            )
        else:
            row.ciphertext = ciphertext
            row.updated_at = now


def secrets_key_configured() -> bool:
    """True when a valid Fernet key is available."""
    return _fernet_from_config() is not None


def secrets_key_source() -> SecretsKeySource:
    """Where the active Fernet key material was loaded from."""
    _material, source = load_secrets_key_material()
    if not _material:
        return "none"
    try:
        Fernet(_material.encode("ascii"))
    except (TypeError, ValueError):
        return "none"
    return source


def tailwind_token_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the Tailwind token."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return False
    with discovery_session(path) as session:
        row = session.scalar(
            select(AppSecret.key).where(AppSecret.key == _TAILWIND_SECRET_KEY)
        )
        return row is not None


def _fernet_from_config() -> Fernet | None:
    try:
        raw, _source = load_secrets_key_material()
    except ValueError as exc:
        raise SecretsConfigurationError(str(exc)) from exc
    if not raw:
        return None
    try:
        return Fernet(raw.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise SecretsConfigurationError(
            "Expected domesti_secrets_key to be a url-safe base64-encoded 32-byte Fernet key"
        ) from exc


def _require_fernet() -> Fernet:
    fernet = _fernet_from_config()
    if fernet is None:
        raise SecretsConfigurationError(
            "Expected domesti_secrets_key in domesti-bot.config.json at the repo root "
            "(gitignored) or DOMESTI_BOT_SECRETS_KEY in the environment before storing "
            "encrypted secrets"
        )
    return fernet
