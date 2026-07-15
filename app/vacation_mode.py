"""Sticky vacation-mode latch: far-from-home hysteresis with transition emails.

Config lives in the automation-rules bundle (``vacation_mode``). While
``enabled`` is true, the latch arms when every configured user remains at least
``min_distance_m`` from home for ``hysteresis_s`` (default 1800), and disarms when
that predicate stops holding for the same dwell. Transition emails go to
``notification_emails`` on both edges.

Restart policy (see also :mod:`app.vacation_mode_store`): persist ``armed`` plus
``far_since`` / ``near_since`` clocks; after boot the next tick reconciles clocks
against the live predicate without dropping a still-valid dwell. When
``enabled`` is false, evaluation is skipped and the persisted latch is left
unchanged (no arm/disarm emails).

Anomaly device alerts while armed are out of scope here (#464).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
from pathlib import Path

from app.api.schemas import VacationModeSettingsOut, normalized_vacation_notification_emails
from app.automation_rules_loader import load_settings_location
from app.device_enums import VacationEmailSource
from app.home_location import try_resolve_home_location
from app.operator_alerts import operator_alert_store
from app.rule_conditions import RuleEvaluationContext, users_min_distance_from_home_met
from app.smtp_service import SmtpConnectionParams, deliver_email_message, smtp_friendly_error
from app.smtp_store import load_smtp_config, resolve_password_for_send, smtp_send_ready
from app.vacation_mode_store import (
    VacationModeStateRecord,
    load_vacation_mode_state,
    save_vacation_mode_state,
)

DEFAULT_VACATION_HYSTERESIS_S = 1800.0
_METERS_PER_MILE = 1609.344

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class VacationModeTickResult:
    """Outcome of one latch evaluation."""

    armed: bool
    far_since: float | None
    near_since: float | None
    transitioned_to: bool | None
    """``True`` arm edge, ``False`` disarm edge, ``None`` no edge."""


def build_vacation_mode_transition_bodies(
    *,
    armed: bool,
    settings: VacationModeSettingsOut,
    source: VacationEmailSource,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for a vacation on/off notification."""
    distance_label = _format_vacation_distance_m(settings.min_distance_m)
    wait_label = _format_vacation_duration_s(settings.hysteresis_s)
    users_label = _format_vacation_user_ids(settings.user_ids)
    home_label = _format_vacation_home_label()
    is_test = source == VacationEmailSource.SETTINGS_TEST

    if armed:
        headline = "Vacation mode is now on."
        why = (
            f"{users_label} stayed at least {distance_label} from {home_label} "
            f"for {wait_label}, so vacation mode turned on."
        )
        wait_key = "Wait before turning on"
    else:
        headline = "Vacation mode is now off."
        why = (
            f"The far-from-home check for {users_label} stopped holding for "
            f"{wait_label} (threshold {distance_label} from {home_label}), "
            "so vacation mode turned off."
        )
        wait_key = "Wait before turning off"

    provenance = _provenance_footer(source)
    facts = [
        f"People: {users_label}",
        f"Distance from home: {distance_label}",
        f"{wait_key}: {wait_label}",
        f"Home: {home_label}",
    ]

    plain_parts: list[str] = []
    if is_test:
        plain_parts.append(
            "This is a test email from Automations → Vacation. "
            "Vacation mode was not actually changed.",
        )
        plain_parts.append("")
    plain_parts.append(headline)
    plain_parts.append("")
    plain_parts.append(why)
    plain_parts.append("")
    plain_parts.extend(facts)
    plain_parts.append("")
    plain_parts.append("—")
    plain_parts.append(provenance)
    plain_body = "\n".join(plain_parts) + "\n"

    html_parts: list[str] = []
    if is_test:
        html_parts.append(
            "<p><strong>Test email</strong> from Automations → Vacation. "
            "Vacation mode was not actually changed.</p>",
        )
    html_parts.append(f"<p><strong>{escape(headline, quote=False)}</strong></p>")
    html_parts.append(f"<p>{escape(why, quote=False)}</p>")
    html_parts.append("<ul>")
    for fact in facts:
        html_parts.append(f"<li>{escape(fact, quote=False)}</li>")
    html_parts.append("</ul>")
    html_parts.append(f"<p><em>{escape(provenance, quote=False)}</em></p>")
    html_body = "".join(html_parts)
    return plain_body, html_body


def evaluate_vacation_mode_tick(
    *,
    all_far: bool,
    hysteresis_s: float,
    now: float,
    state: VacationModeStateRecord,
) -> VacationModeTickResult:
    """Advance hysteresis clocks and return the next latch state."""
    if hysteresis_s < 1.0:
        raise ValueError(f"Expected hysteresis_s >= 1.0, got {hysteresis_s}")

    armed = state.armed
    far_since = state.far_since
    near_since = state.near_since
    transitioned_to: bool | None = None

    if all_far:
        near_since = None
        if far_since is None:
            far_since = now
        if not armed and (now - far_since) >= hysteresis_s:
            armed = True
            transitioned_to = True
    else:
        far_since = None
        if near_since is None:
            near_since = now
        if armed and (now - near_since) >= hysteresis_s:
            armed = False
            transitioned_to = False

    return VacationModeTickResult(
        armed=armed,
        far_since=far_since,
        near_since=near_since,
        transitioned_to=transitioned_to,
    )


def send_vacation_mode_transition_email(
    cache_path: Path,
    *,
    armed: bool,
    settings: VacationModeSettingsOut,
    source: VacationEmailSource = VacationEmailSource.LATCH,
) -> bool:
    """Email configured recipients that vacation mode turned on or off.

    Returns whether a message was handed to SMTP. Missing recipients or SMTP
    skips send (logged); SMTP transport failures raise after recording an
    operator alert.
    """
    recipients = normalized_vacation_notification_emails(settings)
    if not recipients:
        _LOGGER.warning(
            "[vacation] transition email skipped — notification_emails is empty "
            "(armed=%s source=%s)",
            armed,
            source.value,
        )
        return False
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        _LOGGER.warning(
            "[vacation] transition email skipped — SMTP is not configured "
            "(armed=%s recipient_count=%d source=%s)",
            armed,
            len(recipients),
            source.value,
        )
        return False
    password = resolve_password_for_send(cache_path, draft_password=None, host=config.host)
    params = SmtpConnectionParams(
        from_address=config.from_address,
        host=config.host,
        mail_domain=config.mail_domain,
        password=password,
        port=config.port,
        username=config.username,
    )
    plain_body, html_body = build_vacation_mode_transition_bodies(
        armed=armed,
        settings=settings,
        source=source,
    )
    if source == VacationEmailSource.SETTINGS_TEST:
        subject = (
            "domesti-bot [test] vacation mode on"
            if armed
            else "domesti-bot [test] vacation mode off"
        )
    elif armed:
        subject = "domesti-bot vacation mode on"
    else:
        subject = "domesti-bot vacation mode off"
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = params.from_address
    message["To"] = ", ".join(recipients)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    try:
        delivery = deliver_email_message(params, message)
    except Exception as exc:
        friendly = smtp_friendly_error(exc, host=params.host)
        operator_alert_store.record_smtp_notification_failure(message=friendly)
        _LOGGER.error(
            "[vacation] transition email failed armed=%s recipient_count=%d "
            "host=%s:%s source=%s: %s",
            armed,
            len(recipients),
            params.host,
            params.port,
            source.value,
            friendly,
        )
        raise
    operator_alert_store.clear_smtp_notification_failure()
    _LOGGER.info(
        "[vacation] transition email sent armed=%s source=%s %s",
        armed,
        source.value,
        delivery.format_for_log(redact_recipients=True),
    )
    return True


def tick_vacation_mode(
    cache_path: Path,
    *,
    ctx: RuleEvaluationContext,
    now: float,
    settings: VacationModeSettingsOut,
) -> VacationModeTickResult | None:
    """Evaluate, persist, and email vacation-mode edges when enabled.

    Returns ``None`` when vacation mode is disabled (latch untouched).
    """
    if not settings.enabled:
        return None
    if not settings.user_ids:
        _LOGGER.warning(
            "[vacation] enabled but user_ids is empty — skipping latch tick",
        )
        return None

    previous = load_vacation_mode_state(cache_path)
    all_far = users_min_distance_from_home_met(
        ctx=ctx,
        min_distance_m=settings.min_distance_m,
        min_location_accuracy_m=settings.min_location_accuracy_m,
        user_ids=settings.user_ids,
    )
    result = evaluate_vacation_mode_tick(
        all_far=all_far,
        hysteresis_s=settings.hysteresis_s,
        now=now,
        state=previous,
    )
    if (
        result.armed != previous.armed
        or result.far_since != previous.far_since
        or result.near_since != previous.near_since
    ):
        save_vacation_mode_state(
            cache_path,
            armed=result.armed,
            far_since=result.far_since,
            near_since=result.near_since,
        )
    if result.transitioned_to is not None:
        _LOGGER.info(
            "[vacation] mode %s (hysteresis_s=%s min_distance_m=%s user_ids=%s)",
            "armed" if result.transitioned_to else "disarmed",
            settings.hysteresis_s,
            settings.min_distance_m,
            ",".join(settings.user_ids),
        )
        try:
            send_vacation_mode_transition_email(
                cache_path,
                armed=result.transitioned_to,
                settings=settings,
                source=VacationEmailSource.LATCH,
            )
        except Exception as exc:
            operator_alert_store.record_smtp_notification_failure(
                message=(
                    "Vacation mode transition email failed after latch update "
                    f"(armed={result.transitioned_to}): {exc}"
                ),
                reason_code="vacation_transition_email_failed",
            )
            _LOGGER.exception(
                "[vacation] transition email raised after latch update armed=%s",
                result.transitioned_to,
            )
    return result


def _format_vacation_distance_m(distance_m: float) -> str:
    if distance_m >= 1000.0:
        km = distance_m / 1000.0
        miles = distance_m / _METERS_PER_MILE
        if abs(km - round(km)) < 1e-6:
            km_label = f"{int(round(km))} km"
        else:
            km_label = f"{km:.1f} km"
        return f"{km_label} (≈ {miles:.0f} mi)"
    if abs(distance_m - round(distance_m)) < 1e-6:
        return f"{int(round(distance_m))} m"
    if distance_m < 1.0:
        return f"{distance_m:.1f} m"
    return f"{distance_m:.0f} m"


def _format_vacation_duration_s(total_s: float) -> str:
    whole = max(0, int(round(total_s)))
    if whole < 60:
        return f"{whole} seconds"
    minutes = whole // 60
    seconds = whole % 60
    if minutes < 60:
        if seconds == 0:
            return f"{minutes} minutes" if minutes != 1 else "1 minute"
        return f"{minutes} min {seconds} sec"
    hours = minutes // 60
    rem_min = minutes % 60
    if rem_min == 0 and seconds == 0:
        return f"{hours} hours" if hours != 1 else "1 hour"
    if seconds == 0:
        return f"{hours} h {rem_min} min"
    return f"{hours} h {rem_min} min {seconds} sec"


def _format_vacation_home_label() -> str:
    try:
        settings = load_settings_location()
        home = try_resolve_home_location(settings)
    except Exception:
        _LOGGER.exception("[vacation] failed to resolve home for transition email")
        return "home (unavailable — could not load settings location)"
    if home is None:
        return "home (not configured — lat/lon are the 0,0 sentinel)"
    name = (home.home_label or "").strip() or "Home"
    return f"{name} ({home.lat:.6f}, {home.lon:.6f})"


def _format_vacation_user_ids(user_ids: list[str]) -> str:
    cleaned = [uid.strip() for uid in user_ids if uid.strip() != ""]
    if not cleaned:
        return "(no users configured)"
    if len(cleaned) == 1:
        return cleaned[0]
    if len(cleaned) == 2:
        return f"{cleaned[0]} and {cleaned[1]}"
    return ", ".join(cleaned[:-1]) + f", and {cleaned[-1]}"


def _provenance_footer(source: VacationEmailSource) -> str:
    match source:
        case VacationEmailSource.SETTINGS_TEST:
            return "Sent by: domesti-bot · Automations → Vacation (test email)"
        case VacationEmailSource.LATCH:
            return "Sent by: domesti-bot · Vacation mode (automatic)"
        case _:
            _LOGGER.warning(
                "[vacation] unknown VacationEmailSource for provenance: %s",
                source.value,
            )
            return f"Sent by: domesti-bot · Vacation mode ({source.value})"
