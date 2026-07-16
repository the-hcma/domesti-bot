"""Shared helpers for outbound email provenance and UI deep links."""

from __future__ import annotations

import logging
import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.mytracks_service import MyTracksSyncError, normalize_public_base_url
from app.mytracks_store import load_mytracks_pair_status

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


def provenance_footer(*, subsystem: str, trigger: str) -> str:
    """Return the canonical outbound-email provenance line."""
    subsystem_label = subsystem.strip()
    trigger_label = trigger.strip()
    if subsystem_label == "":
        raise ValueError("Expected non-empty subsystem for provenance footer")
    if trigger_label == "":
        raise ValueError("Expected non-empty trigger for provenance footer")
    return f"Sent by: domesti-bot · {subsystem_label} ({trigger_label})"


def rule_fire_provenance_footer(rule_id: str) -> str:
    """Provenance for a rule ``notify_on_fire`` notification."""
    cleaned = rule_id.strip()
    if cleaned == "":
        raise ValueError("Expected non-empty rule_id for rule-fire provenance")
    return provenance_footer(subsystem=f"Rule {cleaned}", trigger="automation")


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
