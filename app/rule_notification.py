"""Rule notification email copy and automation UI deep links."""

from __future__ import annotations

import os
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.api.schemas import RuleOut
from app.mytracks_service import MyTracksSyncError, normalize_public_base_url
from app.mytracks_store import load_mytracks_pair_status
from app.rule_device_action_outcome import RuleDeviceActionOutcome


def _safe_normalize_public_base_url(url: str) -> str | None:
    """Return a normalized public base URL, or ``None`` when config is invalid."""
    try:
        return normalize_public_base_url(url)
    except MyTracksSyncError:
        return None


def build_rule_notification_bodies(
    rule: RuleOut,
    *,
    cache_path: Path | None,
    device_action_outcomes: tuple[RuleDeviceActionOutcome, ...] = (),
    notification_detail: str | None = None,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for a rule fire notification."""
    status_url = rule_automation_status_url(cache_path, rule.id)
    plain_parts = [
        f'The automation rule "{rule.label}" ({rule.id}) just fired.',
        "",
    ]
    if notification_detail:
        plain_parts.extend([notification_detail, ""])
    device_lines = format_device_action_outcomes(device_action_outcomes)
    if device_lines:
        plain_parts.append("Device actions:")
        plain_parts.extend(f"- {line}" for line in device_lines)
        plain_parts.append("")
    if status_url is not None:
        plain_parts.append(f"View live status: {status_url}")
    else:
        plain_parts.append(
            "Open Automations → Status in domesti-bot for live condition details.",
        )

    safe_label = escape(rule.label, quote=False)
    safe_id = escape(rule.id, quote=False)
    html_parts = [
        f"<p>The automation rule <strong>{safe_label}</strong> "
        f"(<code>{safe_id}</code>) just fired.</p>",
    ]
    if notification_detail:
        safe_detail = escape(notification_detail, quote=False).replace("\n", "<br>")
        html_parts.append(f"<p>{safe_detail}</p>")
    if device_lines:
        html_parts.append("<p><strong>Device actions</strong></p><ul>")
        for line in device_lines:
            html_parts.append(f"<li>{escape(line, quote=False)}</li>")
        html_parts.append("</ul>")
    if status_url is not None:
        safe_url = escape(status_url, quote=True)
        html_parts.append(
            f'<p><a href="{safe_url}">View live status for {safe_label}</a></p>',
        )
    else:
        html_parts.append(
            "<p>Open Automations → Status in domesti-bot for live condition "
            "details.</p>",
        )
    return "\n".join(plain_parts), "".join(html_parts)


def domesti_public_base_url(cache_path: Path | None) -> str | None:
    """Resolve the browser-facing origin for automation UI links."""
    env = (os.environ.get("DOMESTI_PUBLIC_BASE_URL") or "").strip()
    if env != "":
        return _safe_normalize_public_base_url(env)
    if cache_path is None:
        return None
    pair_status = load_mytracks_pair_status(cache_path)
    if pair_status is None or pair_status.domesti_public_base_url is None:
        return None
    return _safe_normalize_public_base_url(pair_status.domesti_public_base_url)


def format_device_action_outcomes(
    outcomes: tuple[RuleDeviceActionOutcome, ...],
) -> tuple[str, ...]:
    """Format per-device before/after state lines for notification email."""
    lines: list[str] = []
    for outcome in outcomes:
        before = outcome.before_state or "unknown"
        after = outcome.after_state or "unknown"
        family = outcome.family_id.display_name()
        line = f"{outcome.device_id} ({family}): {before} → {after}"
        if outcome.error is not None:
            line = f"{line} — failed: {outcome.error}"
        elif outcome.probable:
            line = f"{line} (probable)"
        lines.append(line)
    return tuple(lines)


def rule_automation_status_url(cache_path: Path | None, rule_id: str) -> str | None:
    """Return a deep link to the rule on the Automations Status tab, if known."""
    base = domesti_public_base_url(cache_path)
    if base is None:
        return None
    slug = quote(rule_id.strip(), safe="")
    if slug == "":
        return None
    return f"{base.rstrip('/')}/#/automations/status/{slug}"
