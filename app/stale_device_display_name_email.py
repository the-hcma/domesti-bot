"""Once-per-day email when automation rules store stale device display names."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AllConditionsCondition,
    AnyConditionsCondition,
    DevicesAllInStateCondition,
    DevicesAnyInStateCondition,
    DevicesAnyInStateForSCondition,
    Ep1ReadingCompareCondition,
    RuleConditionOut,
    RuleOut,
    normalized_vacation_notification_emails,
)
from app.automation_rules_loader import (
    AutomationRulesLoadError,
    list_automation_rules,
    load_settings_location,
    load_vacation_mode_settings,
)
from app.cron_schedule import fired_on_same_local_calendar_day
from app.device_display import format_device_display
from app.device_enums import (
    DeviceFamilyId,
    OperatorDigestId,
    StaleDeviceDisplayNameEmailSource,
)
from app.domesti_bot_cli import DeviceManagersState
from app.operator_digest_store import (
    complete_operator_digest_send,
    load_operator_digest_last_sent_at,
    release_operator_digest_claim,
    try_claim_operator_digest_for_local_day,
)
from app.outbound_email import (
    append_provenance_footer,
    build_outbound_message,
    clear_outbound_smtp_failure,
    deliver_outbound_email,
    domesti_public_base_url,
    load_outbound_smtp_params,
    provenance_footer,
    record_outbound_smtp_failure,
)
from app.rule_actions import lookup_preferred_label
from app.rule_notification import rule_automation_status_url
from app.rule_validation import (
    RosterUserRow,
    RuleValidationContext,
    build_roster_name_hint_lookup,
    build_roster_user_id_lookup,
    stale_device_display_name_issue,
)
from app.rules_store import list_geofences, list_users
from app.smtp_store import load_smtp_config, smtp_send_ready

_LOGGER = logging.getLogger(__name__)

STALE_DISPLAY_NAME_DIGEST_FINDING_TEMPLATE = (
    '{device_label}: stored display_name "{stored}" no longer matches the live label'
)
STALE_DISPLAY_NAME_DIGEST_INSTANCE_PREFIX = "Instance:"
STALE_DISPLAY_NAME_DIGEST_INTRO = (
    "One or more automation rules still store a friendly device name that no longer "
    "matches the live label on the device. The MAC (device id) is still correct — "
    "update the display_name snapshot so Automations stays readable."
)
STALE_DISPLAY_NAME_DIGEST_STATUS_LINK_TEXT = "View rule status"
STALE_DISPLAY_NAME_DIGEST_SUBJECT = "Stale device labels in automation rules"
STALE_DISPLAY_NAME_DIGEST_SUBSYSTEM = "Automations → device labels"


@dataclass(frozen=True)
class StaleDisplayNameFinding:
    """One stale ``display_name`` warning tied to a rule."""

    device_id: str
    live_display_name: str
    rule_id: str
    rule_label: str
    stored_display_name: str


def build_stale_display_name_digest_bodies(
    findings: tuple[StaleDisplayNameFinding, ...],
    *,
    cache_path: Path | None,
    source: StaleDeviceDisplayNameEmailSource = StaleDeviceDisplayNameEmailSource.AUTOMATIC,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for the stale display-name digest."""
    if not findings:
        raise ValueError("Expected at least one stale display-name finding, got none")
    plain_parts = [STALE_DISPLAY_NAME_DIGEST_INTRO, "", "Details:"]
    html_parts = [
        f"<p>{escape(STALE_DISPLAY_NAME_DIGEST_INTRO, quote=False)}</p>",
        "<p><strong>Details</strong></p>",
        "<ul>",
    ]
    for finding in findings:
        status_url = rule_automation_status_url(cache_path, finding.rule_id)
        heading = f"{finding.rule_label} ({finding.rule_id})"
        device_label = format_device_display(finding.device_id, finding.live_display_name)
        detail = STALE_DISPLAY_NAME_DIGEST_FINDING_TEMPLATE.format(
            device_label=device_label,
            stored=finding.stored_display_name,
        )
        plain_parts.append(f"- {heading}: {detail}")
        if status_url is not None:
            plain_parts.append(f"  {STALE_DISPLAY_NAME_DIGEST_STATUS_LINK_TEXT}: {status_url}")
        safe_heading = escape(heading, quote=False)
        safe_detail = escape(detail, quote=False)
        if status_url is not None:
            safe_href = escape(status_url, quote=True)
            safe_link = escape(STALE_DISPLAY_NAME_DIGEST_STATUS_LINK_TEXT, quote=False)
            html_parts.append(
                f'<li><strong>{safe_heading}</strong>: {safe_detail} — <a href="{safe_href}">{safe_link}</a></li>'
            )
        else:
            html_parts.append(f"<li><strong>{safe_heading}</strong>: {safe_detail}</li>")
    html_parts.append("</ul>")
    instance_url = domesti_public_base_url(cache_path)
    if instance_url is not None:
        plain_parts.extend(["", f"{STALE_DISPLAY_NAME_DIGEST_INSTANCE_PREFIX} {instance_url}"])
        html_parts.append(
            f"<p>{escape(STALE_DISPLAY_NAME_DIGEST_INSTANCE_PREFIX, quote=False)} "
            f'<a href="{escape(instance_url, quote=True)}">{escape(instance_url, quote=False)}</a></p>'
        )
    else:
        plain_parts.extend(
            [
                "",
                "Open Automations → Status in domesti-bot to review Warning badges on rules.",
            ]
        )
        html_parts.append("<p>Open Automations → Status in domesti-bot to review Warning badges on rules.</p>")
    append_provenance_footer(
        plain_parts,
        html_parts,
        provenance=provenance_footer(
            subsystem=STALE_DISPLAY_NAME_DIGEST_SUBSYSTEM,
            trigger=source.value,
        ),
    )
    return "\n".join(plain_parts), "\n".join(html_parts)


def collect_stale_display_name_findings(
    rules: list[RuleOut],
    ctx: RuleValidationContext,
) -> tuple[StaleDisplayNameFinding, ...]:
    """Return stale ``display_name`` snapshots across ``rules`` (stable, deduped)."""
    findings: list[StaleDisplayNameFinding] = []
    seen: set[tuple[str, str, str]] = set()
    if ctx.device_state is None:
        return ()
    for rule in sorted(rules, key=lambda row: row.id):
        for family_id, device_id, stored in _iter_rule_display_snapshots(rule):
            key = (rule.id, device_id, stored.casefold())
            if key in seen:
                continue
            seen.add(key)
            issue = stale_device_display_name_issue(
                ctx,
                family_id=family_id,
                device_id=device_id,
                display_name=stored,
            )
            if issue is None:
                continue
            live = lookup_preferred_label(
                ctx.device_state,
                family_id=family_id,
                device_id=device_id,
            )
            if live is None or live.strip() == "":
                continue
            findings.append(
                StaleDisplayNameFinding(
                    device_id=device_id,
                    live_display_name=live.strip(),
                    rule_id=rule.id,
                    rule_label=rule.label,
                    stored_display_name=stored,
                )
            )
    return tuple(findings)


def maybe_send_stale_display_name_digest(
    *,
    cache_path: Path | None,
    device_state: DeviceManagersState | None,
    now_epoch: float | None = None,
    validation_ctx: RuleValidationContext | None = None,
) -> bool:
    """Send at most one stale-label digest per local day when SMTP is ready.

    Returns True when an email was sent. Skips when there are no findings, SMTP
    is not configured, recipients are empty, discovery is not ready, or a digest
    was already sent today (home timezone). The once-per-day gate is claimed
    atomically before SMTP so concurrent evaluators cannot double-send.
    """
    if cache_path is None:
        return False
    if device_state is None and validation_ctx is None:
        return False
    clock = time.time() if now_epoch is None else now_epoch
    try:
        settings = load_settings_location()
        timezone = ZoneInfo(settings.timezone)
    except Exception:
        _LOGGER.exception("[rules] stale display-name digest skipped — failed to load timezone")
        return False
    last_sent = load_operator_digest_last_sent_at(
        cache_path,
        OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
    )
    if fired_on_same_local_calendar_day(last_sent, clock, timezone):
        return False
    smtp_record = load_smtp_config(cache_path)
    if smtp_record is None or not smtp_send_ready(smtp_record):
        _LOGGER.debug("[rules] stale display-name digest skipped — SMTP is not configured")
        return False
    try:
        recipients = list(normalized_vacation_notification_emails(load_vacation_mode_settings()))
    except AutomationRulesLoadError:
        _LOGGER.exception(
            "[rules] stale display-name digest skipped — failed to load vacation settings",
        )
        return False
    if not recipients:
        _LOGGER.debug(
            "[rules] stale display-name digest skipped — vacation notification_emails is empty",
        )
        return False
    try:
        rules = list_automation_rules()
    except AutomationRulesLoadError:
        _LOGGER.exception("[rules] stale display-name digest skipped — failed to load rules")
        return False
    ctx = validation_ctx
    if ctx is None:
        ctx = _build_validation_context(cache_path=cache_path, device_state=device_state)
    findings = collect_stale_display_name_findings(rules, ctx)
    if not findings:
        return False
    claim_token = try_claim_operator_digest_for_local_day(
        cache_path,
        digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        now_epoch=clock,
        timezone=timezone,
    )
    if claim_token is None:
        return False
    try:
        sent = send_stale_display_name_digest(
            findings,
            cache_path=cache_path,
            recipients=recipients,
        )
    except Exception:
        release_operator_digest_claim(
            cache_path,
            claim_token=claim_token,
            digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        )
        raise
    if not sent:
        release_operator_digest_claim(
            cache_path,
            claim_token=claim_token,
            digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        )
        return False
    completed_at = time.time()
    complete_operator_digest_send(
        cache_path,
        claim_token=claim_token,
        digest_id=OperatorDigestId.STALE_DEVICE_DISPLAY_NAME,
        last_sent_at=completed_at,
        local_date=datetime.fromtimestamp(completed_at, tz=timezone).date().isoformat(),
    )
    return True


def send_stale_display_name_digest(
    findings: tuple[StaleDisplayNameFinding, ...],
    *,
    cache_path: Path,
    recipients: list[str],
    source: StaleDeviceDisplayNameEmailSource = StaleDeviceDisplayNameEmailSource.AUTOMATIC,
) -> bool:
    """Deliver the digest via SMTP. Persistence is owned by the caller claim/complete path."""
    params = load_outbound_smtp_params(cache_path)
    if params is None:
        _LOGGER.debug("[rules] stale display-name digest skipped — SMTP params unavailable")
        return False
    plain_body, html_body = build_stale_display_name_digest_bodies(
        findings,
        cache_path=cache_path,
        source=source,
    )
    message = build_outbound_message(
        from_address=params.from_address,
        html_body=html_body,
        plain_body=plain_body,
        subject=STALE_DISPLAY_NAME_DIGEST_SUBJECT,
        to_addresses=recipients,
    )
    try:
        result = deliver_outbound_email(params, message)
    except Exception as exc:
        friendly = record_outbound_smtp_failure(exc, host=params.host)
        _LOGGER.error(
            "[rules] stale display-name digest failed recipient_count=%d host=%s:%s: %s",
            len(recipients),
            params.host,
            params.port,
            friendly,
        )
        return False
    clear_outbound_smtp_failure()
    _LOGGER.info(
        "[rules] stale display-name digest sent finding_count=%d recipient_count=%d %s",
        len(findings),
        len(recipients),
        result.format_for_log(redact_recipients=True),
    )
    return True


def _build_validation_context(
    *,
    cache_path: Path,
    device_state: DeviceManagersState | None,
) -> RuleValidationContext:
    users = list_users(cache_path)
    geofences = list_geofences(cache_path)
    roster_users = [
        RosterUserRow(
            display_name=row.display_name,
            first_name=row.first_name,
            user_id=row.user_id,
        )
        for row in users
    ]
    smtp_record = load_smtp_config(cache_path)
    return RuleValidationContext(
        device_state=device_state,
        geofence_ids=frozenset(row.geofence_id for row in geofences),
        roster_name_hint_lookup=build_roster_name_hint_lookup(roster_users),
        roster_user_id_lookup=build_roster_user_id_lookup([row.user_id for row in users]),
        smtp_configured=smtp_send_ready(smtp_record),
    )


def _iter_condition_display_snapshots(
    conditions: list[RuleConditionOut],
) -> list[tuple[DeviceFamilyId, str, str]]:
    found: list[tuple[DeviceFamilyId, str, str]] = []
    for condition in conditions:
        if isinstance(
            condition,
            (
                DevicesAllInStateCondition,
                DevicesAnyInStateCondition,
                DevicesAnyInStateForSCondition,
            ),
        ):
            for ref in condition.devices:
                stored = (ref.display_name or "").strip()
                if stored:
                    found.append((ref.family_id, ref.device_id.strip(), stored))
        elif isinstance(condition, Ep1ReadingCompareCondition):
            stored = (condition.device.display_name or "").strip()
            if stored:
                found.append(
                    (
                        condition.device.family_id,
                        condition.device.device_id.strip(),
                        stored,
                    )
                )
        elif isinstance(condition, AllConditionsCondition):
            found.extend(_iter_condition_display_snapshots(condition.conditions))
        elif isinstance(condition, AnyConditionsCondition):
            found.extend(_iter_condition_display_snapshots(condition.conditions))
    return found


def _iter_rule_display_snapshots(
    rule: RuleOut,
) -> list[tuple[DeviceFamilyId, str, str]]:
    found: list[tuple[DeviceFamilyId, str, str]] = []
    for action in rule.device_actions:
        stored = (action.display_name or "").strip()
        if stored:
            found.append((action.family_id, action.device_id.strip(), stored))
    found.extend(_iter_condition_display_snapshots(rule.conditions.all))
    return found
