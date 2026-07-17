"""Rule notification email copy and automation UI deep links."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote

from app.api.schemas import RuleOut
from app.outbound_email import (
    append_provenance_footer,
    domesti_public_base_url,
    rule_fire_provenance_footer,
    with_instance_hash,
)
from app.rule_device_action_outcome import RuleDeviceActionOutcome
from app.rule_engine import expected_state_for_action_type


@dataclass(frozen=True)
class DeviceActionEmailSummary:
    """Device-action section for a rule fire notification email."""

    changed_lines: tuple[str, ...]
    no_change_message: str | None


# Public message constants (asserted by tests — do not hard-code prose there).
RULE_FIRE_ACTIONS_CANCELLED_NOTE = "Remaining delayed device actions were cancelled before they ran."
RULE_FIRE_COMPLETED_SEQUENCE_TEMPLATE = (
    'The automation rule "{label}" ({rule_id}) completed its device-action sequence.'
)
RULE_FIRE_JUST_FIRED_TEMPLATE = 'The automation rule "{label}" ({rule_id}) just fired.'
RULE_FIRE_TIMELINE_HEADING = "Timeline"


def build_rule_notification_bodies(
    rule: RuleOut,
    *,
    cache_path: Path | None,
    cancelled_remaining: bool = False,
    device_action_outcomes: tuple[RuleDeviceActionOutcome, ...] = (),
    notification_detail: str | None = None,
    sequence_completed: bool = False,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for a rule fire notification."""
    status_url = rule_automation_status_url(cache_path, rule.id)
    device_summary = summarize_device_action_outcomes(device_action_outcomes)
    intro = (
        RULE_FIRE_COMPLETED_SEQUENCE_TEMPLATE.format(label=rule.label, rule_id=rule.id)
        if sequence_completed
        else RULE_FIRE_JUST_FIRED_TEMPLATE.format(label=rule.label, rule_id=rule.id)
    )
    plain_parts = [intro, ""]
    if notification_detail:
        plain_parts.extend([notification_detail, ""])
    if device_summary.changed_lines or device_summary.no_change_message is not None:
        plain_parts.append(f"{RULE_FIRE_TIMELINE_HEADING}:")
        if device_summary.changed_lines:
            plain_parts.extend(f"- {line}" for line in device_summary.changed_lines)
        else:
            assert device_summary.no_change_message is not None
            plain_parts.append(device_summary.no_change_message)
        plain_parts.append("")
    if cancelled_remaining:
        plain_parts.extend([RULE_FIRE_ACTIONS_CANCELLED_NOTE, ""])
    if status_url is not None:
        plain_parts.append(f"View live status: {status_url}")
    else:
        plain_parts.append(
            "Open Automations → Status in domesti-bot for live condition details.",
        )
    instance_url = domesti_public_base_url(cache_path)
    if instance_url is not None:
        plain_parts.append(f"Instance: {instance_url}")

    html_parts = [f"<p>{escape(intro, quote=False)}</p>"]
    if notification_detail:
        safe_detail = escape(notification_detail, quote=False).replace("\n", "<br>")
        html_parts.append(f"<p>{safe_detail}</p>")
    if device_summary.changed_lines or device_summary.no_change_message is not None:
        html_parts.append(f"<p><strong>{escape(RULE_FIRE_TIMELINE_HEADING, quote=False)}</strong></p>")
        if device_summary.changed_lines:
            html_parts.append("<ul>")
            for line in device_summary.changed_lines:
                html_parts.append(f"<li>{escape(line, quote=False)}</li>")
            html_parts.append("</ul>")
        else:
            assert device_summary.no_change_message is not None
            safe_message = escape(device_summary.no_change_message, quote=False)
            html_parts.append(f"<p>{safe_message}</p>")
    if cancelled_remaining:
        html_parts.append(f"<p>{escape(RULE_FIRE_ACTIONS_CANCELLED_NOTE, quote=False)}</p>")
    if status_url is not None:
        safe_label = escape(rule.label, quote=False)
        safe_url = escape(status_url, quote=True)
        html_parts.append(
            f'<p><a href="{safe_url}">View live status for {safe_label}</a></p>',
        )
    else:
        html_parts.append(
            "<p>Open Automations → Status in domesti-bot for live condition details.</p>",
        )
    if instance_url is not None:
        safe_instance = escape(instance_url, quote=True)
        html_parts.append(
            f'<p>Instance: <a href="{safe_instance}">{safe_instance}</a></p>',
        )
    append_provenance_footer(
        plain_parts,
        html_parts,
        provenance=rule_fire_provenance_footer(rule.id),
    )
    return "\n".join(plain_parts) + "\n", "".join(html_parts)


def format_device_action_outcome_line(outcome: RuleDeviceActionOutcome) -> str:
    """Format one device outcome line for notification email."""
    family = outcome.family_id.display_name()
    when = _format_completed_at_local(outcome.completed_at)
    if not outcome.succeeded:
        before = outcome.before_state or "unknown"
        after = outcome.after_state or "unknown"
        detail = f": {outcome.error}" if outcome.error else ""
        if before == after:
            return f"{outcome.device_id} ({family}): failed{detail} at {when}"
        return f"{outcome.device_id} ({family}): {before} → {after} — failed{detail} at {when}"
    before = outcome.before_state or "unknown"
    after = outcome.after_state or "unknown"
    line = f"{outcome.device_id} ({family}): {before} → {after}"
    if outcome.probable:
        line = f"{line} (probable)"
    return f"{line} at {when}"


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
    return with_instance_hash(base, f"#/automations/status/{slug}")


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


def _device_state_changed(outcome: RuleDeviceActionOutcome) -> bool:
    before = outcome.before_state
    after = outcome.after_state
    if before is None or after is None:
        return before != after
    return before != after


def _format_completed_at_local(completed_at: float) -> str:
    """Format an action completion epoch in the system local timezone."""

    return datetime.fromtimestamp(completed_at).strftime("%Y-%m-%d %H:%M:%S")
