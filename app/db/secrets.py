"""Encrypted application secrets stored in the discovery database."""

from __future__ import annotations

import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select

from app.db.models import AppSecret
from app.db.secrets_key import SecretsKeySource, load_secrets_key_material
from app.db.session import discovery_session

_SMTP_PASSWORD_KEY = "smtp_password"
_MYTRACKS_ADMIN_PASSWORD_KEY = "mytracks_admin_password"
_MYTRACKS_RELAY_API_KEY = "mytracks_relay_api_key"
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


def load_smtp_password_from_db(path: Path) -> str | None:
    """Return the decrypted SMTP password from the database, or ``None``."""
    return _load_app_secret_plaintext(path, _SMTP_PASSWORD_KEY)


def load_mytracks_admin_password_from_db(path: Path) -> str | None:
    """Return the decrypted My Tracks admin password, or ``None``."""
    return _load_app_secret_plaintext(path, _MYTRACKS_ADMIN_PASSWORD_KEY)


def load_mytracks_relay_api_key_from_db(path: Path) -> str | None:
    """Return the decrypted my-tracks relay API key, or ``None``."""
    return _load_app_secret_plaintext(path, _MYTRACKS_RELAY_API_KEY)


def load_tailwind_token_from_db(path: Path) -> str | None:
    """Return the decrypted Tailwind token from the database, or ``None``."""
    token = _load_app_secret_plaintext(path, _TAILWIND_SECRET_KEY)
    if token is None:
        return None
    stripped = token.strip()
    return stripped if stripped else None


def load_vizio_auth_hosts_from_db(path: Path) -> list[str]:
    """Return TV host strings that have encrypted SmartCast auth rows."""
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return []
    prefix = "vizio_auth:"
    with discovery_session(path) as session:
        rows = session.scalars(
            select(AppSecret.key).where(AppSecret.key.like(f"{prefix}%"))
        )
        hosts = [str(key)[len(prefix) :] for key in rows if str(key).startswith(prefix)]
    return sorted(set(h.strip() for h in hosts if h.strip()))


def load_vizio_auth_token_from_db(path: Path, *, host: str) -> str | None:
    """Return the decrypted SmartCast auth token for ``host``, or ``None``."""
    token = _load_app_secret_plaintext(path, _vizio_auth_secret_key(host))
    if token is None:
        return None
    stripped = token.strip()
    return stripped if stripped else None


def save_smtp_password_to_db(path: Path, password: str) -> None:
    """Encrypt and persist the SMTP password."""
    _save_app_secret_plaintext(path, _SMTP_PASSWORD_KEY, password)


def save_mytracks_admin_password_to_db(path: Path, password: str) -> None:
    """Encrypt and persist the My Tracks admin password."""
    _save_app_secret_plaintext(path, _MYTRACKS_ADMIN_PASSWORD_KEY, password)


def save_mytracks_relay_api_key_to_db(path: Path, api_key: str) -> None:
    """Encrypt and persist the my-tracks relay API key."""
    _save_app_secret_plaintext(path, _MYTRACKS_RELAY_API_KEY, api_key.strip())


def save_tailwind_token_to_db(path: Path, token: str) -> None:
    """Encrypt and persist the Tailwind Local Control Key."""
    _save_app_secret_plaintext(path, _TAILWIND_SECRET_KEY, token.strip())


def save_vizio_auth_token_to_db(path: Path, *, host: str, token: str) -> None:
    """Encrypt and persist a per-TV SmartCast auth token."""
    _save_app_secret_plaintext(path, _vizio_auth_secret_key(host), token.strip())


def smtp_password_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the SMTP password."""
    return _app_secret_stored_in_db(path, _SMTP_PASSWORD_KEY)


def mytracks_admin_password_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the My Tracks admin password."""
    return _app_secret_stored_in_db(path, _MYTRACKS_ADMIN_PASSWORD_KEY)


def mytracks_relay_api_key_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the my-tracks relay API key."""
    return _app_secret_stored_in_db(path, _MYTRACKS_RELAY_API_KEY)


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
    return _app_secret_stored_in_db(path, _TAILWIND_SECRET_KEY)


def vizio_auth_token_stored_in_db(path: Path, *, host: str) -> bool:
    """True when an ``app_secrets`` row exists for the given TV host."""
    return _app_secret_stored_in_db(path, _vizio_auth_secret_key(host))


def _app_secret_stored_in_db(path: Path, key: str) -> bool:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return False
    with discovery_session(path) as session:
        row = session.scalar(select(AppSecret.key).where(AppSecret.key == key))
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


def _load_app_secret_plaintext(path: Path, key: str) -> str | None:
    fernet = _fernet_from_config()
    if fernet is None:
        return None
    with discovery_session(path) as session:
        row = session.get(AppSecret, key)
        if row is None:
            return None
        try:
            plain = fernet.decrypt(row.ciphertext)
        except InvalidToken as exc:
            raise SecretsDecryptError(
                f"Expected valid Fernet ciphertext for {key}, got undecryptable data"
            ) from exc
        text = plain.decode("utf-8")
        return text if text else None


def _require_fernet() -> Fernet:
    fernet = _fernet_from_config()
    if fernet is None:
        raise SecretsConfigurationError(
            "Expected domesti_secrets_key in domesti-bot.config.json at the repo root "
            "(gitignored) or DOMESTI_BOT_SECRETS_KEY in the environment before storing "
            "encrypted secrets"
        )
    return fernet


def _save_app_secret_plaintext(path: Path, key: str, value: str) -> None:
    fernet = _require_fernet()
    ciphertext = fernet.encrypt(value.encode("utf-8"))
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(AppSecret, key)
        if row is None:
            session.add(
                AppSecret(
                    key=key,
                    ciphertext=ciphertext,
                    updated_at=now,
                )
            )
        else:
            row.ciphertext = ciphertext
            row.updated_at = now


def _vizio_auth_secret_key(host: str) -> str:
    return f"vizio_auth:{host.strip()}"
