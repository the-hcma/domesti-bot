"""Tests for SMTP settings persistence helpers."""

from __future__ import annotations

from pathlib import Path

from app.smtp_store import SmtpConfigRecord, smtp_send_ready


def test_smtp_send_ready_accepts_unauthenticated_relay_without_password() -> None:
    record = SmtpConfigRecord(
        from_address="domestibot-noreply@db.hcma.info",
        host="localhost",
        last_test_recipient="ops@hcma.info",
        mail_domain="db.hcma.info",
        password_configured=False,
        port=25,
        username="",
    )
    assert smtp_send_ready(record) is True


def test_smtp_send_ready_requires_password_when_username_set() -> None:
    record = SmtpConfigRecord(
        from_address="domestibot-noreply@hcma.info",
        host="smtp.example.com",
        last_test_recipient=None,
        mail_domain="hcma.info",
        password_configured=False,
        port=587,
        username="mailer",
    )
    assert smtp_send_ready(record) is False

    with_password = SmtpConfigRecord(
        from_address=record.from_address,
        host=record.host,
        last_test_recipient=record.last_test_recipient,
        mail_domain=record.mail_domain,
        password_configured=True,
        port=record.port,
        username=record.username,
    )
    assert smtp_send_ready(with_password) is True


def test_smtp_send_ready_rejects_missing_row(tmp_path: Path) -> None:
    del tmp_path
    assert smtp_send_ready(None) is False
