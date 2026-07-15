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
from pathlib import Path

from app.api.schemas import VacationModeSettingsOut, normalized_vacation_notification_emails
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

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class VacationModeTickResult:
    """Outcome of one latch evaluation."""

    armed: bool
    far_since: float | None
    near_since: float | None
    transitioned_to: bool | None
    """``True`` arm edge, ``False`` disarm edge, ``None`` no edge."""


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
            "(armed=%s)",
            armed,
        )
        return False
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        _LOGGER.warning(
            "[vacation] transition email skipped — SMTP is not configured "
            "(armed=%s recipient_count=%d)",
            armed,
            len(recipients),
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
    if armed:
        subject = "domesti-bot vacation mode on"
        plain_body = (
            "Vacation mode armed: all configured users have remained far from "
            "home for the configured hysteresis window.\n"
        )
    else:
        subject = "domesti-bot vacation mode off"
        plain_body = (
            "Vacation mode disarmed: the far-from-home condition no longer held "
            "for the configured hysteresis window.\n"
        )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = params.from_address
    message["To"] = ", ".join(recipients)
    message.set_content(plain_body)
    try:
        delivery = deliver_email_message(params, message)
    except Exception as exc:
        friendly = smtp_friendly_error(exc, host=params.host)
        operator_alert_store.record_smtp_notification_failure(message=friendly)
        _LOGGER.error(
            "[vacation] transition email failed armed=%s recipient_count=%d "
            "host=%s:%s: %s",
            armed,
            len(recipients),
            params.host,
            params.port,
            friendly,
        )
        raise
    operator_alert_store.clear_smtp_notification_failure()
    _LOGGER.info(
        "[vacation] transition email sent armed=%s %s",
        armed,
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
            )
        except Exception:
            _LOGGER.exception(
                "[vacation] transition email raised after latch update armed=%s",
                result.transitioned_to,
            )
    return result
