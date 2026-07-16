"""Shared helpers for outbound email provenance, MIME assembly, and delivery."""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.mytracks_service import MyTracksSyncError, normalize_public_base_url
from app.mytracks_store import load_mytracks_pair_status
from app.operator_alerts import operator_alert_store
from app.smtp_service import (
    SmtpConnectionParams,
    SmtpDeliveryResult,
    deliver_email_message,
    resolve_instance_url,
    smtp_friendly_error,
)
from app.smtp_store import load_smtp_config, resolve_password_for_send, smtp_send_ready

_LOGGER = logging.getLogger(__name__)


def append_provenance_footer(
    plain_parts: list[str],
    html_parts: list[str],
    *,
    provenance: str,
) -> None:
    """Append the standard plain/HTML provenance footer to in-progress body parts."""
    plain_parts.extend(["", "—", provenance])
    html_parts.append(f"<p><em>{escape(provenance, quote=False)}</em></p>")


def automations_mail_url(cache_path: Path | None) -> str | None:
    """Deep link to Automations → Mail when a public base URL is configured."""
    return _automations_tab_url(cache_path, "mail")


def automations_vacation_url(cache_path: Path | None) -> str | None:
    """Deep link to Automations → Vacation when a public base URL is configured."""
    return _automations_tab_url(cache_path, "vacation")


def build_outbound_message(
    *,
    from_address: str,
    html_body: str,
    plain_body: str,
    subject: str,
    to_addresses: Sequence[str],
) -> EmailMessage:
    """Assemble a multipart/alternative ``EmailMessage`` (plain + HTML)."""
    recipients = [address.strip() for address in to_addresses if address.strip() != ""]
    if not recipients:
        raise ValueError("Expected at least one recipient email address, got none")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_address
    message["To"] = ", ".join(recipients)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    return message


def clear_outbound_smtp_failure() -> None:
    """Clear the operator SMTP-notification failure alert after a successful send."""
    operator_alert_store.clear_smtp_notification_failure()


def deliver_outbound_email(
    params: SmtpConnectionParams,
    message: EmailMessage,
) -> SmtpDeliveryResult:
    """Hand a prepared message to the SMTP transport."""
    return deliver_email_message(params, message)


def domesti_public_base_url(cache_path: Path | None) -> str | None:
    """Resolve the browser-facing origin for dashboard / Automations UI links."""
    env = (os.environ.get("DOMESTI_PUBLIC_BASE_URL") or "").strip()
    if env != "":
        return _safe_normalize_public_base_url(env)
    if cache_path is None:
        return None
    pair_status = load_mytracks_pair_status(cache_path)
    if pair_status is None or pair_status.domesti_public_base_url is None:
        return None
    return _safe_normalize_public_base_url(pair_status.domesti_public_base_url)


def format_ui_link_html(*, href: str, label: str) -> str:
    """Return an HTML paragraph with a single anchored UI link."""
    safe_href = escape(href, quote=True)
    safe_label = escape(label, quote=False)
    return f'<p><a href="{safe_href}">{safe_label}</a></p>'


def format_ui_link_plain(*, href: str, label: str) -> str:
    """Return a plain-text UI link line."""
    return f"{label}: {href}"


def load_outbound_smtp_params(cache_path: Path) -> SmtpConnectionParams | None:
    """Return SMTP connection params when config is present and send-ready."""
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        return None
    password = resolve_password_for_send(cache_path, draft_password=None, host=config.host)
    return SmtpConnectionParams(
        from_address=config.from_address,
        host=config.host,
        mail_domain=config.mail_domain,
        password=password,
        port=config.port,
        username=config.username,
    )


def provenance_footer(*, subsystem: str, trigger: str) -> str:
    """Return the canonical outbound-email provenance line."""
    subsystem_label = subsystem.strip()
    trigger_label = trigger.strip()
    if subsystem_label == "":
        raise ValueError("Expected non-empty subsystem for provenance footer")
    if trigger_label == "":
        raise ValueError("Expected non-empty trigger for provenance footer")
    return f"Sent by: domesti-bot · {subsystem_label} ({trigger_label})"


def record_outbound_smtp_failure(exc: Exception, *, host: str) -> str:
    """Record an operator alert for a failed notification send; return friendly text."""
    friendly = smtp_friendly_error(exc, host=host)
    operator_alert_store.record_smtp_notification_failure(message=friendly)
    return friendly


def rule_fire_provenance_footer(rule_id: str) -> str:
    """Provenance for a rule ``notify_on_fire`` notification."""
    cleaned = rule_id.strip()
    if cleaned == "":
        raise ValueError("Expected non-empty rule_id for rule-fire provenance")
    return provenance_footer(subsystem=f"Rule {cleaned}", trigger="automation")


def send_test_email(
    params: SmtpConnectionParams,
    *,
    instance_url: str | None = None,
    to_address: str,
) -> None:
    """Send a Settings → Mail test message. Raises on failure."""
    recipient = to_address.strip()
    if recipient == "":
        raise ValueError("Expected recipient email, got empty value")
    dashboard_url = resolve_instance_url(
        instance_url=instance_url,
        mail_domain=params.mail_domain,
    )
    mail_settings_url = ""
    if dashboard_url != "":
        mail_settings_url = with_instance_hash(dashboard_url, "#/automations/mail")
    plain_lines = [
        "This is a test message from domesti-bot Settings → Mail.",
        "",
        "SMTP is configured correctly.",
        "",
        "No live state was changed.",
    ]
    html_lines = [
        "<p>This is a test message from domesti-bot Settings → Mail.</p>",
        "<p>SMTP is configured correctly.</p>",
        "<p>No live state was changed.</p>",
    ]
    if mail_settings_url != "":
        plain_lines.extend(
            [
                "",
                f"Instance: {dashboard_url.rstrip('/')}",
                "",
                f"Open Automations → Mail: {mail_settings_url}",
            ]
        )
        safe_dash = escape(dashboard_url.rstrip("/"), quote=True)
        safe_url = escape(mail_settings_url, quote=True)
        html_lines.append(
            f'<p>Instance: <a href="{safe_dash}">{safe_dash}</a></p>',
        )
        html_lines.append(
            f'<p><a href="{safe_url}">Open Automations → Mail</a></p>',
        )
    elif dashboard_url != "":
        plain_lines.extend(["", f"Instance: {dashboard_url}"])
        safe_url = escape(dashboard_url, quote=True)
        html_lines.append(
            f'<p>Instance: <a href="{safe_url}">{safe_url}</a></p>',
        )
    append_provenance_footer(
        plain_lines,
        html_lines,
        provenance=provenance_footer(
            subsystem="Settings → Mail",
            trigger="test email",
        ),
    )
    message = build_outbound_message(
        from_address=params.from_address,
        html_body="".join(html_lines),
        plain_body="\n".join(plain_lines) + "\n",
        subject="domesti-bot [test] SMTP configuration",
        to_addresses=[recipient],
    )
    delivery = deliver_outbound_email(params, message)
    _LOGGER.info(
        "SMTP test email sent %s",
        delivery.format_for_log(),
    )


def with_instance_hash(base_url: str, hash_path: str) -> str:
    """Join a public origin with a ``#/…`` deep-link path.

    ``hash_path`` may be passed with or without a leading ``#``; a bare path
    such as ``/automations/vacation`` is normalized to ``#/automations/vacation``.

    Example: ``https://home.example.com`` + ``#/automations/vacation`` →
    ``https://home.example.com/#/automations/vacation``.
    """
    base = base_url.rstrip("/")
    path = hash_path if hash_path.startswith("#") else f"#{hash_path}"
    return f"{base}/{path}"


def _automations_tab_url(cache_path: Path | None, tab: str) -> str | None:
    base = domesti_public_base_url(cache_path)
    if base is None:
        return None
    slug = quote(tab.strip(), safe="")
    if slug == "":
        return None
    return with_instance_hash(base, f"#/automations/{slug}")


def _safe_normalize_public_base_url(url: str) -> str | None:
    """Return a normalized public base URL, or ``None`` when config is invalid."""
    try:
        return normalize_public_base_url(url)
    except MyTracksSyncError as exc:
        _LOGGER.warning(
            "Ignoring invalid public base URL for outbound email links: %s",
            exc,
        )
        return None
