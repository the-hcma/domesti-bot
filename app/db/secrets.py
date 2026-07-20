"""Encrypted application secrets stored in the discovery database."""

from __future__ import annotations

import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSecret
from app.db.secrets_key import SecretsKeySource, load_secrets_key_material
from app.db.session import discovery_session, discovery_write
from app.vizio_mac import normalize_mac

_EP1_NOISE_PSK_KEY = "ep1_noise_psk"
_KASA_PASSWORD_KEY = "kasa_password"
_KASA_USERNAME_KEY = "kasa_username"
_MYTRACKS_ADMIN_PASSWORD_KEY = "mytracks_admin_password"
_MYTRACKS_RELAY_API_KEY = "mytracks_relay_api_key"
_SMTP_PASSWORD_KEY = "smtp_password"
_TAILWIND_SECRET_KEY = "tailwind_token"


class SecretsConfigurationError(ValueError):
    """Raised when no valid Fernet key is configured."""


class SecretsDecryptError(ValueError):
    """Raised when ciphertext cannot be decrypted with the configured key."""


def delete_app_secret(path: Path, *, key: str) -> None:
    """Remove one secret row if present."""

    def _write(session: Session) -> None:
        row = session.get(AppSecret, key.strip())
        if row is not None:
            session.delete(row)

    discovery_write(path, _write)


def delete_kasa_credentials_from_db(path: Path) -> None:
    """Remove encrypted Kasa account username and password rows atomically."""

    def _write(session: Session) -> None:
        for key in (_KASA_PASSWORD_KEY, _KASA_USERNAME_KEY):
            row = session.get(AppSecret, key)
            if row is not None:
                session.delete(row)

    discovery_write(path, _write)


def load_ep1_noise_psk_from_db(path: Path) -> str | None:
    """Return the decrypted EP1 Noise PSK from the database, or ``None``."""
    psk = _load_app_secret_plaintext(path, _EP1_NOISE_PSK_KEY)
    if psk is None:
        return None
    stripped = psk.strip()
    return stripped if stripped else None


def load_kasa_credentials_from_db(path: Path) -> tuple[str, str] | None:
    """Return ``(username, password)`` when both encrypted rows decrypt, else ``None``."""
    username = _load_app_secret_plaintext(path, _KASA_USERNAME_KEY)
    password = _load_app_secret_plaintext(path, _KASA_PASSWORD_KEY)
    if username is None or password is None:
        return None
    un = username.strip()
    pw = password.strip()
    if not un or not pw:
        return None
    return un, pw


def load_mytracks_admin_password_from_db(path: Path) -> str | None:
    """Return the decrypted My Tracks admin password, or ``None``."""
    return _load_app_secret_plaintext(path, _MYTRACKS_ADMIN_PASSWORD_KEY)


def load_mytracks_relay_api_key_from_db(path: Path) -> str | None:
    """Return the decrypted my-tracks relay API key, or ``None``."""
    return _load_app_secret_plaintext(path, _MYTRACKS_RELAY_API_KEY)


def load_smtp_password_from_db(path: Path) -> str | None:
    """Return the decrypted SMTP password from the database, or ``None``."""
    return _load_app_secret_plaintext(path, _SMTP_PASSWORD_KEY)


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
        rows = session.scalars(select(AppSecret.key).where(AppSecret.key.like(f"{prefix}%")))
        hosts = [str(key)[len(prefix) :] for key in rows if str(key).startswith(prefix)]
    return sorted(set(h.strip() for h in hosts if h.strip()))


def load_vizio_auth_token_from_db(
    path: Path,
    *,
    mac: str | None = None,
    host: str | None = None,
) -> str | None:
    """Return the decrypted SmartCast auth token for ``mac`` or legacy ``host``."""
    if mac:
        token = _load_app_secret_plaintext(path, _vizio_auth_secret_key_mac(mac))
        if token is not None:
            stripped = token.strip()
            if stripped:
                return stripped
    if host:
        token = _load_app_secret_plaintext(path, _vizio_auth_secret_key_host(host))
        if token is None:
            return None
        stripped = token.strip()
        return stripped if stripped else None
    return None


def save_ep1_noise_psk_to_db(path: Path, psk: str) -> None:
    """Encrypt and persist the EP1 ESPHome Noise PSK."""
    _save_app_secret_plaintext(path, _EP1_NOISE_PSK_KEY, psk.strip())


def save_kasa_credentials_to_db(
    path: Path,
    *,
    username: str,
    password: str,
) -> None:
    """Encrypt and persist Kasa/Tapo account email and password (both required).

    Username and password are written in one ``discovery_write`` commit so a
    crash cannot leave a single orphaned credential row.
    """
    un = username.strip()
    pw = password.strip()
    if not un or not pw:
        raise ValueError(
            "Expected non-empty Kasa account email and password, got "
            f"username={un!r} password={'<set>' if pw else '<empty>'}"
        )
    fernet = _require_fernet()
    now = time.time()

    def _write(session: Session) -> None:
        for key, value in ((_KASA_PASSWORD_KEY, pw), (_KASA_USERNAME_KEY, un)):
            ciphertext = fernet.encrypt(value.encode("utf-8"))
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

    discovery_write(path, _write)


def save_mytracks_admin_password_to_db(path: Path, password: str) -> None:
    """Encrypt and persist the My Tracks admin password."""
    _save_app_secret_plaintext(path, _MYTRACKS_ADMIN_PASSWORD_KEY, password)


def save_mytracks_relay_api_key_to_db(path: Path, api_key: str) -> None:
    """Encrypt and persist the my-tracks relay API key."""
    _save_app_secret_plaintext(path, _MYTRACKS_RELAY_API_KEY, api_key.strip())


def save_smtp_password_to_db(path: Path, password: str) -> None:
    """Encrypt and persist the SMTP password."""
    _save_app_secret_plaintext(path, _SMTP_PASSWORD_KEY, password)


def save_tailwind_token_to_db(path: Path, token: str) -> None:
    """Encrypt and persist the Tailwind Local Control Key."""
    _save_app_secret_plaintext(path, _TAILWIND_SECRET_KEY, token.strip())


def save_vizio_auth_token_to_db(
    path: Path,
    *,
    token: str,
    mac: str | None = None,
    host: str | None = None,
) -> None:
    """Encrypt and persist a per-TV SmartCast auth token (prefer ``mac`` key)."""
    stripped = token.strip()
    if mac:
        _save_app_secret_plaintext(path, _vizio_auth_secret_key_mac(mac), stripped)
        if host:
            delete_app_secret(path, key=_vizio_auth_secret_key_host(host))
        return
    if host:
        _save_app_secret_plaintext(path, _vizio_auth_secret_key_host(host), stripped)
        return
    raise ValueError("Expected mac or host for Vizio auth token storage, got neither")


def ep1_noise_psk_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the EP1 Noise PSK."""
    return _app_secret_stored_in_db(path, _EP1_NOISE_PSK_KEY)


def kasa_credentials_stored_in_db(path: Path) -> bool:
    """True when both Kasa username and password rows exist."""
    return _app_secret_stored_in_db(path, _KASA_USERNAME_KEY) and _app_secret_stored_in_db(
        path,
        _KASA_PASSWORD_KEY,
    )


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


def smtp_password_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the SMTP password."""
    return _app_secret_stored_in_db(path, _SMTP_PASSWORD_KEY)


def tailwind_token_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the Tailwind token."""
    return _app_secret_stored_in_db(path, _TAILWIND_SECRET_KEY)


def vizio_auth_token_stored_in_db(
    path: Path,
    *,
    mac: str | None = None,
    host: str | None = None,
) -> bool:
    """True when an ``app_secrets`` row exists for the given TV MAC or legacy host."""
    if mac and _app_secret_stored_in_db(path, _vizio_auth_secret_key_mac(mac)):
        return True
    if host:
        return _app_secret_stored_in_db(path, _vizio_auth_secret_key_host(host))
    return False


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
            raise SecretsDecryptError(f"Expected valid Fernet ciphertext for {key}, got undecryptable data") from exc
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

    def _write(session: Session) -> None:
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

    discovery_write(path, _write)


def _vizio_auth_secret_key_host(host: str) -> str:
    return f"vizio_auth:{host.strip()}"


def _vizio_auth_secret_key_mac(mac: str) -> str:
    return f"vizio_auth:{normalize_mac(mac)}"
