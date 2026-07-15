"""SMTP connectivity helpers for operator test sends and future rule notifications."""

from __future__ import annotations

import logging
import re
import smtplib
import socket
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape

_LOGGER = logging.getLogger(__name__)

_SMTP_TIMEOUT_S = 10.0
_QUEUE_ID_RE = re.compile(r"queued as ([0-9A-Za-z]+)", re.IGNORECASE)


@dataclass(frozen=True)
class SmtpDeliveryResult:
    """Outcome of handing a message to the configured SMTP relay."""

    host: str
    port: int
    recipients: tuple[str, ...]
    smtp_code: int
    smtp_response: str

    def format_for_log(self, *, redact_recipients: bool = False) -> str:
        parts = [
            f"smtp={self.smtp_code}",
            f"host={self.host}:{self.port}",
        ]
        if redact_recipients:
            parts.insert(0, f"recipient_count={len(self.recipients)}")
        else:
            parts.insert(0, f"to={','.join(self.recipients)}")
        queue_id = self.queue_id
        if queue_id is not None:
            parts.append(f"queue_id={queue_id}")
        response = self.smtp_response.strip()
        if response:
            parts.append(f"response={response!r}")
        return " ".join(parts)

    @property
    def queue_id(self) -> str | None:
        match = _QUEUE_ID_RE.search(self.smtp_response)
        if match is None:
            return None
        return match.group(1)


@dataclass(frozen=True)
class SmtpConnectionParams:
    from_address: str
    host: str
    mail_domain: str
    password: str
    port: int
    username: str


class _LoggingSmtpMixin:
    smtp_data_code: int
    smtp_data_response: str

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.smtp_data_code = 0
        self.smtp_data_response = ""

    def data(self, msg: bytes | str) -> tuple[int, bytes]:
        code, repl = super().data(msg)  # type: ignore[misc]
        self.smtp_data_code = code
        self.smtp_data_response = _decode_smtp_response(repl)
        return code, repl


class _LoggingSMTP(_LoggingSmtpMixin, smtplib.SMTP):
    pass


class _LoggingSMTPSSL(_LoggingSmtpMixin, smtplib.SMTP_SSL):
    pass


def _decode_smtp_response(raw: bytes | str) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _deliver_message(smtp: smtplib.SMTP, message: EmailMessage) -> None:
    refused = smtp.send_message(message)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)


def _maybe_login(smtp: smtplib.SMTP, params: SmtpConnectionParams) -> None:
    if params.username == "" and params.password == "":
        return
    smtp.login(params.username, params.password)


def _message_recipients(message: EmailMessage) -> tuple[str, ...]:
    to_header = message.get("To", "")
    recipients = [part.strip() for part in to_header.split(",") if part.strip()]
    return tuple(recipients)


def _send_message(
    params: SmtpConnectionParams,
    message: EmailMessage,
) -> SmtpDeliveryResult:
    recipients = _message_recipients(message)
    use_ssl = params.port == 465
    if use_ssl:
        with _LoggingSMTPSSL(
            params.host,
            params.port,
            timeout=_SMTP_TIMEOUT_S,
        ) as smtp:
            _maybe_login(smtp, params)
            _deliver_message(smtp, message)
            smtp_code = smtp.smtp_data_code
            smtp_response = smtp.smtp_data_response
        return SmtpDeliveryResult(
            host=params.host,
            port=params.port,
            recipients=recipients,
            smtp_code=smtp_code,
            smtp_response=smtp_response,
        )
    with _LoggingSMTP(params.host, params.port, timeout=_SMTP_TIMEOUT_S) as smtp:
        if params.port in (587, 2525):
            smtp.starttls()
        _maybe_login(smtp, params)
        _deliver_message(smtp, message)
        smtp_code = smtp.smtp_data_code
        smtp_response = smtp.smtp_data_response
    return SmtpDeliveryResult(
        host=params.host,
        port=params.port,
        recipients=recipients,
        smtp_code=smtp_code,
        smtp_response=smtp_response,
    )


def deliver_email_message(
    params: SmtpConnectionParams,
    message: EmailMessage,
) -> SmtpDeliveryResult:
    """Send a prepared email message using the given SMTP connection parameters."""
    return _send_message(params, message)


def instance_url_from_mail_domain(mail_domain: str) -> str:
    """Build the public dashboard URL for the configured instance domain."""
    domain = mail_domain.strip().rstrip("/")
    if domain == "":
        return ""
    return f"https://{domain}/"


def normalize_instance_url(url: str) -> str:
    """Ensure a trailing slash; return empty when input is blank."""
    trimmed = url.strip()
    if trimmed == "":
        return ""
    return trimmed if trimmed.endswith("/") else f"{trimmed}/"


def resolve_instance_url(
    *,
    instance_url: str | None,
    mail_domain: str,
) -> str:
    """Prefer the live UI origin; fall back to ``https://{mail_domain}/``."""
    resolved = normalize_instance_url(instance_url or "")
    if resolved != "":
        return resolved
    return instance_url_from_mail_domain(mail_domain)


def send_test_email(
    params: SmtpConnectionParams,
    *,
    instance_url: str | None = None,
    to_address: str,
) -> None:
    """Send a test message using the given connection parameters. Raises on failure."""
    recipient = to_address.strip()
    if recipient == "":
        raise ValueError("Expected recipient email, got empty value")
    dashboard_url = resolve_instance_url(
        instance_url=instance_url,
        mail_domain=params.mail_domain,
    )
    message = EmailMessage()
    message["Subject"] = "domesti-bot SMTP test"
    message["From"] = params.from_address
    message["To"] = recipient
    plain_lines = [
        "SMTP is configured correctly. This is a test message from domesti-bot.",
    ]
    html_lines = [
        "<p>SMTP is configured correctly. This is a test message from domesti-bot.</p>",
    ]
    if dashboard_url != "":
        plain_lines.append(f"Open your dashboard: {dashboard_url}")
        safe_url = escape(dashboard_url, quote=True)
        html_lines.append(
            f'<p>Open your dashboard: <a href="{safe_url}">{safe_url}</a></p>',
        )
    provenance = "Sent by: domesti-bot · Settings → Mail (test email)"
    plain_lines.extend(["", "—", provenance])
    html_lines.append(f"<p><em>{escape(provenance, quote=False)}</em></p>")
    message.set_content("\n\n".join(plain_lines) + "\n")
    message.add_alternative("".join(html_lines), subtype="html")
    delivery = _send_message(params, message)
    _LOGGER.info(
        "SMTP test email sent %s",
        delivery.format_for_log(),
    )


def smtp_friendly_error(exc: Exception, *, host: str = "") -> str:
    """Translate low-level socket/SMTP exceptions into readable messages."""
    msg = str(exc)
    host_label = f" '{host}'" if host else ""
    if isinstance(exc, socket.gaierror):
        return (
            f"Could not resolve hostname{host_label} — check that the SMTP host is correct."
        )
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"Connection to{host_label} was refused — verify the host and port are correct "
            "and that no firewall is blocking the connection."
        )
    if isinstance(exc, TimeoutError):
        return (
            f"Connection to{host_label} timed out — verify the host and port are correct "
            "and that no firewall is blocking the connection."
        )
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return f"Authentication failed — check the username and password. ({msg})"
    if isinstance(exc, smtplib.SMTPNotSupportedError) and "AUTH" in msg:
        return (
            "The server does not support SMTP authentication. "
            "If this is an unauthenticated relay (e.g. a local or internal mail server), "
            "leave Username and Password blank."
        )
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return (
            "The server does not support a required feature — check your TLS/SSL settings. "
            f"({msg})"
        )
    if isinstance(exc, smtplib.SMTPConnectError):
        return (
            f"Could not connect to the server{host_label} — verify the host and port are "
            f"correct and the server is reachable. ({msg})"
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return (
            f"SMTP relay refused {len(exc.recipients)} recipient(s) — "
            "verify notification addresses are valid."
        )
    if isinstance(exc, smtplib.SMTPException):
        return f"SMTP error: {msg}"
    return msg
