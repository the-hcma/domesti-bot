"""Unit tests for SMTP test-email helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from app.smtp_service import (
    SmtpConnectionParams,
    instance_url_from_mail_domain,
    send_test_email,
)


def test_instance_url_from_mail_domain_strips_whitespace_and_slash() -> None:
    assert instance_url_from_mail_domain(" hcma.info/ ") == "https://hcma.info/"
    assert instance_url_from_mail_domain("") == ""


@patch("app.smtp_service.smtplib.SMTP")
def test_send_test_email_includes_instance_dashboard_link(smtp_cls: MagicMock) -> None:
    smtp_instance = MagicMock()
    smtp_cls.return_value.__enter__.return_value = smtp_instance
    params = SmtpConnectionParams(
        from_address="domestibot-noreply@hcma.info",
        host="localhost",
        mail_domain="hcma.info",
        password="",
        port=25,
        username="",
    )

    send_test_email(params, to_address="ops@hcma.info")

    smtp_instance.send_message.assert_called_once()
    message = smtp_instance.send_message.call_args[0][0]
    payload = message.as_string()
    assert "https://hcma.info/" in payload
    assert "<a href" in payload
