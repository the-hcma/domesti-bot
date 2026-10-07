"""Encrypted application secrets stored in the discovery database."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AppSecret
from app.db.secrets_key import SecretsKeySource, load_secrets_key_material, parse_secrets_key_list
from app.db.session import discovery_session, discovery_write
from app.vizio_mac import normalize_mac

_LOGGER = logging.getLogger(__name__)

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


@dataclass(frozen=True)
class SecretsRotationResult:
    """Outcome of :func:`rotate_app_secrets`: row names only, never values."""

    already_current: list[str] = field(default_factory=list)
    rotated: list[str] = field(default_factory=list)
    undecryptable: list[str] = field(default_factory=list)


def _audit_secret_change(action: str, key: str) -> None:
    """Record that a secret changed (which one and how, never the value) for the audit trail."""
    _LOGGER.info("secret %s key=%s", action, key)


def delete_app_secret(path: Path, *, key: str) -> None:
    """Remove one secret row if present."""
    removed = False

    def _write(session: Session) -> None:
        nonlocal removed
        row = session.get(AppSecret, key.strip())
        if row is not None:
            session.delete(row)
            removed = True

    discovery_write(path, _write)
    if removed:
        _audit_secret_change("removed", key.strip())


def delete_kasa_credentials_from_db(path: Path) -> None:
    """Remove encrypted Kasa account username and password rows atomically."""
    removed: list[str] = []

    def _write(session: Session) -> None:
        for key in (_KASA_PASSWORD_KEY, _KASA_USERNAME_KEY):
            row = session.get(AppSecret, key)
            if row is not None:
                session.delete(row)
                removed.append(key)

    discovery_write(path, _write)
    for key in removed:
        _audit_secret_change("removed", key)


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


def rotate_app_secrets(path: Path, *, skip_undecryptable: bool = False) -> SecretsRotationResult:
    """Re-encrypt every ``app_secrets`` row under the newest configured key, in one transaction.

    Rows already under the newest key are left untouched, and ``updated_at`` is never changed (it
    records when the operator last wrote the value, not when the ciphertext was refreshed). A row
    no configured key can decrypt aborts the whole run, leaving every row as it was, unless
    ``skip_undecryptable`` is set, in which case it is reported and left alone.
    """
    fernets = _fernets_from_config()
    if not fernets:
        raise SecretsConfigurationError(
            "Expected a configured Fernet key (domesti_secrets_key or DOMESTI_BOT_SECRETS_KEY) before rotating secrets"
        )
    newest = fernets[0]
    multi = MultiFernet(fernets)
    already_current: list[str] = []
    rotated: list[str] = []
    undecryptable: list[str] = []

    def _write(session: Session) -> None:
        already_current.clear()
        rotated.clear()
        undecryptable.clear()
        for row in session.scalars(select(AppSecret).order_by(AppSecret.key)):
            try:
                newest.decrypt(row.ciphertext)
            except InvalidToken:
                pass
            else:
                already_current.append(row.key)
                continue
            try:
                row.ciphertext = multi.rotate(row.ciphertext)
            except InvalidToken:
                undecryptable.append(row.key)
                continue
            rotated.append(row.key)
        if undecryptable and not skip_undecryptable:
            raise SecretsDecryptError(
                f"Expected every stored secret to decrypt with a configured key, got undecryptable rows: "
                f"{', '.join(undecryptable)}; nothing was changed"
            )

    discovery_write(path, _write)
    for name in rotated:
        _audit_secret_change("re-encrypted", name)
    return SecretsRotationResult(
        already_current=list(already_current),
        rotated=list(rotated),
        undecryptable=list(undecryptable),
    )


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
    actions: dict[str, str] = {}

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
                actions[key] = "created"
            else:
                row.ciphertext = ciphertext
                row.updated_at = now
                actions[key] = "replaced"

    discovery_write(path, _write)
    for key, action in actions.items():
        _audit_secret_change(action, key)


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


def ep1_noise_psk_updated_at(path: Path) -> float | None:
    """Epoch seconds the stored EP1 Noise PSK was last written, without decrypting it."""
    return _app_secret_updated_at(path, _EP1_NOISE_PSK_KEY)


def ep1_noise_psk_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the EP1 Noise PSK."""
    return _app_secret_stored_in_db(path, _EP1_NOISE_PSK_KEY)


def kasa_credentials_updated_at(path: Path) -> float | None:
    """Epoch seconds the stored Kasa credentials were last written, without decrypting them."""
    return _app_secret_updated_at(path, _KASA_PASSWORD_KEY)


def kasa_credentials_stored_in_db(path: Path) -> bool:
    """True when both Kasa username and password rows exist."""
    return _app_secret_stored_in_db(path, _KASA_USERNAME_KEY) and _app_secret_stored_in_db(
        path,
        _KASA_PASSWORD_KEY,
    )


def mytracks_admin_password_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the My Tracks admin password."""
    return _app_secret_stored_in_db(path, _MYTRACKS_ADMIN_PASSWORD_KEY)


def mytracks_relay_api_key_updated_at(path: Path) -> float | None:
    """Epoch seconds the my-tracks relay key was last written (pairing), without decrypting it."""
    return _app_secret_updated_at(path, _MYTRACKS_RELAY_API_KEY)


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
        _validated_fernets(_material)
    except SecretsConfigurationError:
        return "none"
    return source


def smtp_password_stored_in_db(path: Path) -> bool:
    """True when an ``app_secrets`` row exists for the SMTP password."""
    return _app_secret_stored_in_db(path, _SMTP_PASSWORD_KEY)


def tailwind_token_updated_at(path: Path) -> float | None:
    """Epoch seconds the stored Tailwind token was last written, without decrypting it."""
    return _app_secret_updated_at(path, _TAILWIND_SECRET_KEY)


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


def vizio_auth_token_updated_at(
    path: Path,
    *,
    mac: str | None = None,
    host: str | None = None,
) -> float | None:
    """Epoch seconds a TV's stored token was last written (MAC key first, then legacy host), without decrypting."""
    if mac:
        updated = _app_secret_updated_at(path, _vizio_auth_secret_key_mac(mac))
        if updated is not None:
            return updated
    if host:
        return _app_secret_updated_at(path, _vizio_auth_secret_key_host(host))
    return None


def _app_secret_updated_at(path: Path, key: str) -> float | None:
    """``updated_at`` of one secret row (``None`` when absent); never reads the ciphertext's plaintext."""
    if not path.expanduser().resolve().is_file():
        return None
    with discovery_session(path) as session:
        row = session.get(AppSecret, key)
        return row.updated_at if row is not None else None


def _app_secret_stored_in_db(path: Path, key: str) -> bool:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        return False
    with discovery_session(path) as session:
        row = session.scalar(select(AppSecret.key).where(AppSecret.key == key))
        return row is not None


def _fernet_from_config() -> MultiFernet | None:
    fernets = _fernets_from_config()
    return MultiFernet(fernets) if fernets else None


def _fernets_from_config() -> list[Fernet]:
    """Every configured Fernet key, newest first (empty when none is configured)."""
    try:
        raw, _source = load_secrets_key_material()
    except ValueError as exc:
        raise SecretsConfigurationError(str(exc)) from exc
    if not raw:
        return []
    return _validated_fernets(raw)


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


def _require_fernet() -> MultiFernet:
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
    created = False

    def _write(session: Session) -> None:
        nonlocal created
        row = session.get(AppSecret, key)
        if row is None:
            session.add(
                AppSecret(
                    key=key,
                    ciphertext=ciphertext,
                    updated_at=now,
                )
            )
            created = True
        else:
            row.ciphertext = ciphertext
            row.updated_at = now

    discovery_write(path, _write)
    _audit_secret_change("created" if created else "replaced", key)


def _validated_fernets(material: str) -> list[Fernet]:
    keys = parse_secrets_key_list(material)
    if not keys:
        raise SecretsConfigurationError("Expected at least one Fernet key in domesti_secrets_key, got none")
    try:
        return [Fernet(key.encode("ascii")) for key in keys]
    except (TypeError, ValueError) as exc:
        raise SecretsConfigurationError(
            "Expected domesti_secrets_key to be a url-safe base64-encoded 32-byte Fernet key "
            "(or several, comma-separated, newest first)"
        ) from exc


def _vizio_auth_secret_key_host(host: str) -> str:
    return f"vizio_auth:{host.strip()}"


def _vizio_auth_secret_key_mac(mac: str) -> str:
    return f"vizio_auth:{normalize_mac(mac)}"
