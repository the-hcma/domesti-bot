"""Unit tests for SMTP test-email helpers."""

from __future__ import annotations

import smtplib
from unittest.mock import MagicMock, patch

from app.smtp_service import (
    SmtpConnectionParams,
    SmtpDeliveryResult,
    instance_url_from_mail_domain,
    normalize_instance_url,
    resolve_instance_url,
    send_test_email,
    smtp_friendly_error,
)


def test_instance_url_from_mail_domain_strips_whitespace_and_slash() -> None:
    assert instance_url_from_mail_domain(" hcma.info/ ") == "https://hcma.info/"
    assert instance_url_from_mail_domain("") == ""


def test_normalize_instance_url_adds_trailing_slash() -> None:
    assert normalize_instance_url("http://192.168.0.5:8003") == "http://192.168.0.5:8003/"
    assert normalize_instance_url("") == ""


def test_resolve_instance_url_prefers_ui_origin() -> None:
    assert (
        resolve_instance_url(
            instance_url="http://192.168.0.5:8003/",
            mail_domain="hcma.info",
        )
        == "http://192.168.0.5:8003/"
    )
    assert (
        resolve_instance_url(instance_url=None, mail_domain="hcma.info")
        == "https://hcma.info/"
    )


@patch("app.smtp_service._LoggingSMTP")
def test_send_test_email_includes_instance_dashboard_link(smtp_cls: MagicMock) -> None:
    smtp_instance = MagicMock()
    smtp_instance.smtp_data_code = 250
    smtp_instance.smtp_data_response = "2.0.0 Ok: queued as TESTQID"
    smtp_instance.send_message.return_value = {}
    smtp_cls.return_value.__enter__.return_value = smtp_instance
    params = SmtpConnectionParams(
        from_address="domestibot-noreply@hcma.info",
        host="localhost",
        mail_domain="hcma.info",
        password="",
        port=25,
        username="",
    )

    send_test_email(
        params,
        instance_url="http://192.168.0.5:8003",
        to_address="ops@hcma.info",
    )

    smtp_instance.send_message.assert_called_once()
    message = smtp_instance.send_message.call_args[0][0]
    payload = message.as_string()
    assert "http://192.168.0.5:8003/" in payload
    assert "<a href" in payload


@patch("app.smtp_service._LoggingSMTPSSL")
def test_send_test_email_uses_smtp_ssl_on_port_465(smtp_ssl_cls: MagicMock) -> None:
    smtp_instance = MagicMock()
    smtp_instance.smtp_data_code = 250
    smtp_instance.smtp_data_response = "2.0.0 Ok: queued as SSLQID"
    smtp_instance.send_message.return_value = {}
    smtp_ssl_cls.return_value.__enter__.return_value = smtp_instance
    params = SmtpConnectionParams(
        from_address="domestibot-noreply@hcma.info",
        host="smtp.example.com",
        mail_domain="hcma.info",
        password="",
        port=465,
        username="",
    )

    send_test_email(params, to_address="ops@hcma.info")

    smtp_ssl_cls.assert_called_once_with(
        "smtp.example.com",
        465,
        timeout=10.0,
    )
    smtp_instance.send_message.assert_called_once()


@patch("app.smtp_service._LoggingSMTP")
def test_send_test_email_uses_starttls_on_port_587(smtp_cls: MagicMock) -> None:
    smtp_instance = MagicMock()
    smtp_instance.smtp_data_code = 250
    smtp_instance.smtp_data_response = "2.0.0 Ok: queued as TLSQID"
    smtp_instance.send_message.return_value = {}
    smtp_cls.return_value.__enter__.return_value = smtp_instance
    params = SmtpConnectionParams(
        from_address="domestibot-noreply@hcma.info",
        host="smtp.example.com",
        mail_domain="hcma.info",
        password="",
        port=587,
        username="",
    )

    send_test_email(params, to_address="ops@hcma.info")

    smtp_cls.assert_called_once_with("smtp.example.com", 587, timeout=10.0)
    smtp_instance.starttls.assert_called_once()
    smtp_instance.send_message.assert_called_once()


def test_smtp_delivery_result_parses_postfix_queue_id() -> None:
    delivery = SmtpDeliveryResult(
        host="localhost",
        port=25,
        recipients=("ops@hcma.info",),
        smtp_code=250,
        smtp_response="2.0.0 Ok: queued as 4Yf8Q51Q020123",
    )
    assert delivery.queue_id == "4Yf8Q51Q020123"
    assert "queue_id=4Yf8Q51Q020123" in delivery.format_for_log()


def test_smtp_delivery_result_redacts_recipients_when_requested() -> None:
    delivery = SmtpDeliveryResult(
        host="localhost",
        port=25,
        recipients=("ops@hcma.info", "alerts@hcma.info"),
        smtp_code=250,
        smtp_response="2.0.0 Ok",
    )
    redacted = delivery.format_for_log(redact_recipients=True)
    assert "recipient_count=2" in redacted
    assert "ops@hcma.info" not in redacted
    assert "to=" in delivery.format_for_log()


def test_smtp_friendly_error_omits_refused_recipient_addresses() -> None:
    exc = smtplib.SMTPRecipientsRefused(
        {"ops@example.com": (550, b"User unknown")},
    )

    message = smtp_friendly_error(exc, host="smtp.example.com")

    assert "ops@example.com" not in message
    assert "refused 1 recipient" in message
