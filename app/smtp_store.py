"""Persist SMTP settings in the discovery SQLite database."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from app.db.models import SmtpSettings
from app.db.secrets import (
    SecretsConfigurationError,
    delete_app_secret,
    load_smtp_password_from_db,
    save_smtp_password_to_db,
    smtp_password_stored_in_db,
)
from app.db.session import discovery_session

_SMTP_SETTINGS_ID = 1


@dataclass(frozen=True)
class SmtpConfigRecord:
    from_address: str
    host: str
    last_test_recipient: str | None
    mail_domain: str
    password_configured: bool
    port: int
    username: str


@dataclass(frozen=True)
class SmtpConfigSave:
    from_address: str
    host: str
    mail_domain: str
    password: str | None
    port: int
    username: str


def delete_smtp_settings(path: Path) -> None:
    """Remove SMTP settings and the stored password."""
    delete_app_secret(path, key="smtp_password")
    with discovery_session(path) as session:
        row = session.get(SmtpSettings, _SMTP_SETTINGS_ID)
        if row is not None:
            session.delete(row)


def load_smtp_config(path: Path) -> SmtpConfigRecord | None:
    """Return stored SMTP settings without the password."""
    with discovery_session(path) as session:
        row = session.get(SmtpSettings, _SMTP_SETTINGS_ID)
        if row is None:
            return None
        return SmtpConfigRecord(
            from_address=row.from_address,
            host=row.host,
            last_test_recipient=row.last_test_recipient,
            mail_domain=row.mail_domain,
            password_configured=smtp_password_stored_in_db(path),
            port=row.port,
            username=row.username,
        )


def load_smtp_password(path: Path) -> str | None:
    """Return the decrypted SMTP password, or ``None`` when unset."""
    return load_smtp_password_from_db(path)


def record_smtp_test_recipient(path: Path, recipient: str) -> None:
    """Persist the last successful test recipient on the settings row."""
    trimmed = recipient.strip()
    with discovery_session(path) as session:
        row = session.get(SmtpSettings, _SMTP_SETTINGS_ID)
        if row is None:
            return
        row.last_test_recipient = trimmed if trimmed else None
        row.updated_at = time.time()


def smtp_send_ready(record: SmtpConfigRecord | None) -> bool:
    """True when stored SMTP settings are sufficient to send mail."""
    if record is None:
        return False
    if (
        record.host.strip() == ""
        or record.from_address.strip() == ""
        or record.mail_domain.strip() == ""
    ):
        return False
    if record.username.strip() == "":
        return True
    return record.password_configured


def save_smtp_config(path: Path, config: SmtpConfigSave) -> SmtpConfigRecord:
    """Upsert SMTP settings and optionally replace the stored password."""
    now = time.time()
    with discovery_session(path) as session:
        row = session.get(SmtpSettings, _SMTP_SETTINGS_ID)
        if row is None:
            row = SmtpSettings(
                id=_SMTP_SETTINGS_ID,
                host=config.host.strip(),
                port=config.port,
                username=config.username.strip(),
                mail_domain=config.mail_domain.strip(),
                from_address=config.from_address.strip(),
                last_test_recipient=None,
                updated_at=now,
            )
            session.add(row)
        else:
            row.host = config.host.strip()
            row.port = config.port
            row.username = config.username.strip()
            row.mail_domain = config.mail_domain.strip()
            row.from_address = config.from_address.strip()
            row.updated_at = now
    if config.password is not None:
        try:
            save_smtp_password_to_db(path, config.password)
        except SecretsConfigurationError:
            raise
    saved = load_smtp_config(path)
    if saved is None:
        raise RuntimeError("Expected SMTP settings after save, got None")
    return saved


def resolve_password_for_send(
    path: Path,
    *,
    draft_password: str | None,
    host: str,
) -> str:
    """Use the draft password when provided; otherwise reuse the stored secret."""
    if draft_password is not None and draft_password != "":
        return draft_password
    stored = load_smtp_password(path)
    if stored is None:
        return ""
    existing = load_smtp_config(path)
    if existing is not None and existing.host == host.strip():
        return stored
    return ""
