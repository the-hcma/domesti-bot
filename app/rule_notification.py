"""Rule notification email copy and automation UI deep links."""

from __future__ import annotations

import os
from dataclasses import dataclass
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.api.schemas import RuleOut
from app.mytracks_service import MyTracksSyncError, normalize_public_base_url
from app.mytracks_store import load_mytracks_pair_status
from app.rule_engine import expected_state_for_action_type
from app.rule_device_action_outcome import RuleDeviceActionOutcome


@dataclass(frozen=True)
class DeviceActionEmailSummary:
    """Device-action section for a rule fire notification email."""

    changed_lines: tuple[str, ...]
    no_change_message: str | None


def _device_state_changed(outcome: RuleDeviceActionOutcome) -> bool:
    before = outcome.before_state
    after = outcome.after_state
    if before is None or after is None:
        return before != after
    return before != after


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
    device_summary = summarize_device_action_outcomes(device_action_outcomes)
    plain_parts = [
        f'The automation rule "{rule.label}" ({rule.id}) just fired.',
        "",
    ]
    if notification_detail:
        plain_parts.extend([notification_detail, ""])
    if device_summary.changed_lines or device_summary.no_change_message is not None:
        plain_parts.append("Device actions:")
        if device_summary.changed_lines:
            plain_parts.extend(f"- {line}" for line in device_summary.changed_lines)
        else:
            assert device_summary.no_change_message is not None
            plain_parts.append(device_summary.no_change_message)
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
    if device_summary.changed_lines or device_summary.no_change_message is not None:
        html_parts.append("<p><strong>Device actions</strong></p>")
        if device_summary.changed_lines:
            html_parts.append("<ul>")
            for line in device_summary.changed_lines:
                html_parts.append(f"<li>{escape(line, quote=False)}</li>")
            html_parts.append("</ul>")
        else:
            assert device_summary.no_change_message is not None
            safe_message = escape(device_summary.no_change_message, quote=False)
            html_parts.append(f"<p>{safe_message}</p>")
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


def format_device_action_outcome_line(outcome: RuleDeviceActionOutcome) -> str:
    """Format one device outcome line for notification email."""
    family = outcome.family_id.display_name()
    if not outcome.succeeded:
        before = outcome.before_state or "unknown"
        after = outcome.after_state or "unknown"
        detail = f": {outcome.error}" if outcome.error else ""
        if before == after:
            return f"{outcome.device_id} ({family}): failed{detail}"
        return f"{outcome.device_id} ({family}): {before} → {after} — failed{detail}"
    before = outcome.before_state or "unknown"
    after = outcome.after_state or "unknown"
    line = f"{outcome.device_id} ({family}): {before} → {after}"
    if outcome.probable:
        line = f"{line} (probable)"
    return line


def format_device_action_outcomes(
    outcomes: tuple[RuleDeviceActionOutcome, ...],
) -> tuple[str, ...]:
    """Format changed or failed device outcomes for notification email."""
    return summarize_device_action_outcomes(outcomes).changed_lines


def format_devices_already_in_desired_state_message(
    outcomes: tuple[RuleDeviceActionOutcome, ...],
) -> str:
    """Return copy when every device was already in the target state."""
    labels = sorted(
        {expected_state_for_action_type(outcome.action) for outcome in outcomes},
    )
    joined = ", ".join(labels)
    return f"All devices already in their desired ({joined}) state."


def rule_automation_status_url(cache_path: Path | None, rule_id: str) -> str | None:
    """Return a deep link to the rule on the Automations Status tab, if known."""
    base = domesti_public_base_url(cache_path)
    if base is None:
        return None
    slug = quote(rule_id.strip(), safe="")
    if slug == "":
        return None
    return f"{base.rstrip('/')}/#/automations/status/{slug}"


def summarize_device_action_outcomes(
    outcomes: tuple[RuleDeviceActionOutcome, ...],
) -> DeviceActionEmailSummary:
    """Summarize device outcomes for email: changes only, or an all-clear line."""
    if not outcomes:
        return DeviceActionEmailSummary(changed_lines=(), no_change_message=None)
    changed_lines = tuple(
        format_device_action_outcome_line(outcome)
        for outcome in outcomes
        if not outcome.succeeded or _device_state_changed(outcome)
    )
    if changed_lines:
        return DeviceActionEmailSummary(
            changed_lines=changed_lines,
            no_change_message=None,
        )
    return DeviceActionEmailSummary(
        changed_lines=(),
        no_change_message=format_devices_already_in_desired_state_message(outcomes),
    )
