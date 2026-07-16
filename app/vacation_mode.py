"""Sticky vacation-mode latch: far-from-home arm, home-geofence disarm.

Config lives in the automation-rules bundle (``vacation_mode``). While
``enabled`` is true, the latch arms when every configured user remains at least
``min_distance_m`` from home for ``hysteresis_s`` (default 1800), and disarms
when any of those users is inside the configured home geofence (explicit
``wifi_home_geofence_id`` or geofences containing settings home lat/lon). Being
closer than ``min_distance_m`` without entering the home geofence does **not**
disarm. When no home geofence resolves, the latch will not arm; if already
armed it fail-safe disarms. Transition emails go to ``notification_emails`` on
both edges when ``notify_on_transition`` is true (default).

While the latch is **armed** and ``enabled`` is true, unmarked device-state
transitions (not covered by UI/rule :mod:`app.expected_device_change` marks)
send anomaly emails to the same recipients (#464). When ``enabled`` is false,
anomaly mail is quiet even if a prior latch row remains armed.

Restart policy (see also :mod:`app.vacation_mode_store`): persist ``armed`` plus
``far_since`` / ``near_since`` clocks; after boot the next tick reconciles clocks
against the live predicate without dropping a still-valid dwell. When
``enabled`` is false, evaluation is skipped and the persisted latch is left
unchanged (no arm/disarm emails).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path

from app.api.schemas import VacationModeSettingsOut, normalized_vacation_notification_emails
from app.automation_rules_loader import (
    load_settings_location,
    load_vacation_mode_settings,
)
from app.device_enums import DeviceFamilyId, VacationEmailSource
from app.expected_device_change import consume_expected_device_change
from app.home_location import try_resolve_home_location
from app.operator_alerts import operator_alert_store
from app.outbound_email import (
    append_provenance_footer,
    automations_vacation_url,
    domesti_public_base_url,
    format_ui_link_html,
    format_ui_link_plain,
    provenance_footer,
)
from app.presence_store import _haversine_m, list_user_locations
from app.rule_conditions import (
    RuleEvaluationContext,
    users_any_inside_home_geofence,
    users_min_distance_from_home_met,
)
from app.smtp_service import SmtpConnectionParams, deliver_email_message, smtp_friendly_error
from app.smtp_store import load_smtp_config, resolve_password_for_send, smtp_send_ready
from app.vacation_mode_store import (
    VacationModeStateRecord,
    load_vacation_mode_state,
    save_vacation_mode_state,
)
from app.wifi_home_presence import home_geofence_ids

DEFAULT_VACATION_ANOMALY_DEBOUNCE_S = 30.0
DEFAULT_VACATION_HYSTERESIS_S = 1800.0
VACATION_SETTINGS_TEST_ANOMALY_DISCLAIMER = "No device state was changed and vacation mode was not updated."
VACATION_SETTINGS_TEST_PREAMBLE = "This is a test email from Automations → Vacation."
VACATION_SETTINGS_TEST_TRANSITION_DISCLAIMER = "Vacation mode was not actually changed."
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


def build_vacation_mode_anomaly_bodies(
    *,
    family_id: DeviceFamilyId,
    device_id: str,
    previous: bool,
    current: bool | None,
    observed_at: datetime,
    source: VacationEmailSource = VacationEmailSource.ANOMALY,
    cache_path: Path | None = None,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for a vacation device-anomaly email."""
    family_label = family_id.display_name()
    prev_label = format_vacation_bool_device_state(family_id, previous)
    next_label = format_vacation_bool_device_state(family_id, current)
    when_label = observed_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    provenance = _provenance_footer(source)
    is_test = source == VacationEmailSource.SETTINGS_TEST
    headline = f"Unexpected {family_label} change while vacation mode is on."
    why = (
        f"{device_id} went from {prev_label} to {next_label}. "
        "domesti-bot did not mark this as a UI or automation action."
    )
    facts = [
        f"Device: {device_id}",
        f"Family: {family_label}",
        f"Change: {prev_label} → {next_label}",
        f"Observed: {when_label}",
    ]
    plain_parts: list[str] = []
    if is_test:
        plain_parts.append(VACATION_SETTINGS_TEST_PREAMBLE)
        plain_parts.append(VACATION_SETTINGS_TEST_ANOMALY_DISCLAIMER)
        plain_parts.append("")
    plain_parts.extend([headline, "", why, "", *facts])
    html_parts: list[str] = []
    if is_test:
        html_parts.append(
            f"<p><strong>Test email</strong> from Automations → Vacation. "
            f"{escape(VACATION_SETTINGS_TEST_ANOMALY_DISCLAIMER, quote=False)}</p>",
        )
    html_parts.extend(
        [
            f"<p><strong>{escape(headline, quote=False)}</strong></p>",
            f"<p>{escape(why, quote=False)}</p>",
            "<ul>",
            *[f"<li>{escape(fact, quote=False)}</li>" for fact in facts],
            "</ul>",
        ],
    )
    _append_vacation_ui_link(plain_parts, html_parts, cache_path=cache_path)
    append_provenance_footer(plain_parts, html_parts, provenance=provenance)
    return "\n".join(plain_parts) + "\n", "".join(html_parts)


def build_vacation_mode_transition_bodies(
    *,
    armed: bool,
    settings: VacationModeSettingsOut,
    source: VacationEmailSource,
    cache_path: Path | None = None,
) -> tuple[str, str]:
    """Return ``(plain_text, html)`` bodies for a vacation on/off notification."""
    users_label = _format_vacation_user_ids(settings.user_ids)
    home_label = _format_vacation_home_label()
    is_test = source == VacationEmailSource.SETTINGS_TEST

    if armed:
        distance_label = _format_vacation_distance_m(settings.min_distance_m)
        wait_label = _format_vacation_duration_s(settings.hysteresis_s)
        headline = "Vacation mode is now on."
        why = (
            f"{users_label} stayed at least {distance_label} from {home_label} "
            f"for {wait_label}, so vacation mode turned on."
        )
        facts = [
            f"People: {users_label}",
            f"Distance from home: {distance_label}",
            f"Wait before turning on: {wait_label}",
            f"Home: {home_label}",
            *_vacation_observed_distance_facts(
                cache_path=cache_path,
                user_ids=settings.user_ids,
            ),
        ]
    else:
        headline = "Vacation mode is now off."
        why = f"At least one of {users_label} entered the home geofence at {home_label}, so vacation mode turned off."
        facts = [
            f"People: {users_label}",
            f"Disarm: home geofence arrival",
            f"Home: {home_label}",
            *_vacation_observed_distance_facts(
                cache_path=cache_path,
                user_ids=settings.user_ids,
            ),
        ]

    provenance = _provenance_footer(source)

    plain_parts: list[str] = []
    if is_test:
        plain_parts.append(VACATION_SETTINGS_TEST_PREAMBLE)
        plain_parts.append(VACATION_SETTINGS_TEST_TRANSITION_DISCLAIMER)
        plain_parts.append("")
    plain_parts.append(headline)
    plain_parts.append("")
    plain_parts.append(why)
    plain_parts.append("")
    plain_parts.extend(facts)

    html_parts: list[str] = []
    if is_test:
        html_parts.append(
            f"<p><strong>Test email</strong> from Automations → Vacation. "
            f"{escape(VACATION_SETTINGS_TEST_TRANSITION_DISCLAIMER, quote=False)}</p>",
        )
    html_parts.append(f"<p><strong>{escape(headline, quote=False)}</strong></p>")
    html_parts.append(f"<p>{escape(why, quote=False)}</p>")
    html_parts.append("<ul>")
    for fact in facts:
        html_parts.append(f"<li>{escape(fact, quote=False)}</li>")
    html_parts.append("</ul>")
    _append_vacation_ui_link(plain_parts, html_parts, cache_path=cache_path)
    append_provenance_footer(plain_parts, html_parts, provenance=provenance)
    return "\n".join(plain_parts) + "\n", "".join(html_parts)


def evaluate_vacation_mode_tick(
    *,
    all_far: bool,
    anyone_home: bool,
    hysteresis_s: float,
    now: float,
    state: VacationModeStateRecord,
) -> VacationModeTickResult:
    """Advance arm dwell and return the next latch state.

    Arms after ``all_far`` holds for ``hysteresis_s``. Disarms immediately when
    ``anyone_home`` (a configured vacation user inside the home geofence). While
    armed, failing ``all_far`` without ``anyone_home`` leaves the latch armed.
    ``near_since`` is cleared; disarm no longer uses a near-home dwell clock.
    """
    if hysteresis_s < 1.0:
        raise ValueError(f"Expected hysteresis_s >= 1.0, got {hysteresis_s}")

    armed = state.armed
    far_since = state.far_since
    near_since: float | None = None
    transitioned_to: bool | None = None

    if anyone_home:
        far_since = None
        if armed:
            armed = False
            transitioned_to = False
    elif not armed:
        if all_far:
            if far_since is None:
                far_since = now
            if (now - far_since) >= hysteresis_s:
                armed = True
                transitioned_to = True
        else:
            far_since = None
    else:
        # Armed and nobody home — stay armed even when not all_far.
        far_since = None

    return VacationModeTickResult(
        armed=armed,
        far_since=far_since,
        near_since=near_since,
        transitioned_to=transitioned_to,
    )


def format_vacation_bool_device_state(
    family_id: DeviceFamilyId,
    state: bool | None,
) -> str:
    """Human label for a watcher bool (or unknown) for the given family."""
    if state is None:
        return "unknown"
    match family_id:
        case DeviceFamilyId.SONOS:
            return "playing" if state else "paused"
        case DeviceFamilyId.TAILWIND:
            return "open" if state else "closed"
        case _:
            return "on" if state else "off"


def handle_vacation_device_anomaly(
    cache_path: Path,
    *,
    family_id: DeviceFamilyId,
    device_id: str,
    previous: bool,
    current: bool | None,
    now_monotonic: float | None = None,
    observed_at: datetime | None = None,
) -> bool:
    """Send an anomaly email when vacation is armed and the change is unmarked.

    Returns whether a message was handed to SMTP. Quiet when disarmed, when
    ``enabled`` is false, when the change was expected (UI/rule mark consumed),
    when recipients/SMTP are missing, or when the per-device anti-storm debounce
    suppresses a repeat. A debounce reservation is rolled back when the send
    does not complete so missed SMTP does not burn the storm window.
    """
    state = load_vacation_mode_state(cache_path)
    if not state.armed:
        return False
    try:
        settings = load_vacation_mode_settings()
    except Exception:
        _LOGGER.exception("[vacation] anomaly email skipped — failed to load settings")
        return False
    if not settings.enabled:
        return False
    clock = time.monotonic() if now_monotonic is None else now_monotonic
    if consume_expected_device_change(family_id, device_id, now=clock):
        _LOGGER.debug(
            "[vacation] anomaly skipped — expected mark family=%s device_id=%s",
            family_id.value,
            device_id,
        )
        return False
    if not _vacation_anomaly_debounce.allow(
        family_id,
        device_id,
        now=clock,
        window_s=DEFAULT_VACATION_ANOMALY_DEBOUNCE_S,
    ):
        _LOGGER.info(
            "[vacation] anomaly email debounce family=%s device_id=%s",
            family_id.value,
            device_id,
        )
        return False
    when = observed_at if observed_at is not None else datetime.now(UTC)
    try:
        sent = send_vacation_mode_anomaly_email(
            cache_path,
            settings=settings,
            family_id=family_id,
            device_id=device_id,
            previous=previous,
            current=current,
            observed_at=when,
        )
    except Exception:
        _vacation_anomaly_debounce.release(family_id, device_id, reserved_at=clock)
        _LOGGER.exception(
            "[vacation] anomaly email raised family=%s device_id=%s",
            family_id.value,
            device_id,
        )
        return False
    if not sent:
        _vacation_anomaly_debounce.release(family_id, device_id, reserved_at=clock)
    return sent


def send_vacation_mode_anomaly_email(
    cache_path: Path,
    *,
    settings: VacationModeSettingsOut,
    family_id: DeviceFamilyId,
    device_id: str,
    previous: bool,
    current: bool | None,
    observed_at: datetime,
    source: VacationEmailSource = VacationEmailSource.ANOMALY,
) -> bool:
    """Email vacation recipients about an unmarked device transition."""
    recipients = normalized_vacation_notification_emails(settings)
    if not recipients:
        _LOGGER.warning(
            "[vacation] anomaly email skipped — notification_emails is empty (family=%s device_id=%s)",
            family_id.value,
            device_id,
        )
        return False
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        _LOGGER.warning(
            "[vacation] anomaly email skipped — SMTP is not configured (family=%s device_id=%s recipient_count=%d)",
            family_id.value,
            device_id,
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
    plain_body, html_body = build_vacation_mode_anomaly_bodies(
        family_id=family_id,
        device_id=device_id,
        previous=previous,
        current=current,
        observed_at=observed_at,
        source=source,
        cache_path=cache_path,
    )
    prev_label = format_vacation_bool_device_state(family_id, previous)
    next_label = format_vacation_bool_device_state(family_id, current)
    subject_core = f"vacation anomaly: {family_id.display_name()} {device_id} {prev_label}→{next_label}"
    if source == VacationEmailSource.SETTINGS_TEST:
        subject = f"domesti-bot [test] {subject_core}"
    else:
        subject = f"domesti-bot {subject_core}"
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
            "[vacation] anomaly email failed family=%s device_id=%s recipient_count=%d host=%s:%s: %s",
            family_id.value,
            device_id,
            len(recipients),
            params.host,
            params.port,
            friendly,
        )
        raise
    operator_alert_store.clear_smtp_notification_failure()
    _LOGGER.info(
        "[vacation] anomaly email sent family=%s device_id=%s source=%s %s",
        family_id.value,
        device_id,
        source.value,
        delivery.format_for_log(redact_recipients=True),
    )
    return True


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
            "[vacation] transition email skipped — notification_emails is empty (armed=%s source=%s)",
            armed,
            source.value,
        )
        return False
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        _LOGGER.warning(
            "[vacation] transition email skipped — SMTP is not configured (armed=%s recipient_count=%d source=%s)",
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
        cache_path=cache_path,
    )
    if source == VacationEmailSource.SETTINGS_TEST:
        subject = "domesti-bot [test] vacation mode on" if armed else "domesti-bot [test] vacation mode off"
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
            "[vacation] transition email failed armed=%s recipient_count=%d host=%s:%s source=%s: %s",
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
    try:
        location_settings = load_settings_location()
    except Exception:
        _LOGGER.exception(
            "[vacation] failed to load settings location for home geofence check",
        )
        home_ids: frozenset[str] = frozenset()
    else:
        home_ids = home_geofence_ids(location_settings, ctx.geofences)
    if not home_ids:
        fail_safe_disarm = False
        if previous.armed:
            _LOGGER.warning(
                "[vacation] armed but no home geofence resolved — fail-safe "
                "disarm (configure wifi_home_geofence_id or a geofence "
                "containing home lat/lon)",
            )
            anyone_home = True
            all_far = False
            fail_safe_disarm = True
        else:
            # Debug: vacation ticks while misconfigured would otherwise spam WARNING.
            _LOGGER.debug(
                "[vacation] no home geofence resolved — skipping arm until "
                "wifi_home_geofence_id or a geofence containing home lat/lon "
                "is configured",
            )
            anyone_home = False
            all_far = False
    else:
        fail_safe_disarm = False
        anyone_home = users_any_inside_home_geofence(
            ctx=ctx,
            min_location_accuracy_m=settings.min_location_accuracy_m,
            user_ids=settings.user_ids,
            home_ids=home_ids,
        )
    result = evaluate_vacation_mode_tick(
        all_far=all_far,
        anyone_home=anyone_home,
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
        # Fail-safe disarm is not a home arrival — do not send the usual
        # "entered the home geofence" transition body.
        if fail_safe_disarm:
            _LOGGER.info(
                "[vacation] transition email skipped — fail-safe disarm (no home geofence)",
            )
        elif settings.notify_on_transition:
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
        else:
            _LOGGER.info(
                "[vacation] transition email skipped — notify_on_transition=false armed=%s",
                result.transitioned_to,
            )
    return result


class _VacationAnomalyDebounce:
    """Minimal per-device anti-storm guard for anomaly emails."""

    def __init__(self) -> None:
        self._last_sent_at: dict[tuple[DeviceFamilyId, str], float] = {}
        self._lock = threading.Lock()

    def allow(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        now: float,
        window_s: float,
    ) -> bool:
        key = (family_id, device_id.strip())
        if key[1] == "":
            return False
        with self._lock:
            last = self._last_sent_at.get(key)
            if last is not None and (now - last) < window_s:
                return False
            self._last_sent_at[key] = now
            return True

    def clear(self) -> None:
        with self._lock:
            self._last_sent_at.clear()

    def release(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        *,
        reserved_at: float,
    ) -> None:
        """Drop a reservation when the matching alert did not send."""
        key = (family_id, device_id.strip())
        if key[1] == "":
            return
        with self._lock:
            if self._last_sent_at.get(key) == reserved_at:
                del self._last_sent_at[key]


def _append_vacation_ui_link(
    plain_parts: list[str],
    html_parts: list[str],
    *,
    cache_path: Path | None,
) -> None:
    instance_url = domesti_public_base_url(cache_path)
    vacation_url = automations_vacation_url(cache_path)
    plain_parts.append("")
    if instance_url is not None:
        plain_parts.append(
            format_ui_link_plain(href=instance_url, label="Instance"),
        )
        html_parts.append(
            format_ui_link_html(href=instance_url, label="Open instance dashboard"),
        )
    if vacation_url is not None:
        plain_parts.append(
            format_ui_link_plain(
                href=vacation_url,
                label="Open Automations → Vacation",
            ),
        )
        html_parts.append(
            format_ui_link_html(
                href=vacation_url,
                label="Open Automations → Vacation",
            ),
        )
        return
    plain_parts.append(
        "Open Automations → Vacation in domesti-bot to review this setting.",
    )
    html_parts.append(
        "<p>Open Automations → Vacation in domesti-bot to review this setting.</p>",
    )


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
        case VacationEmailSource.ANOMALY:
            return provenance_footer(
                subsystem="Vacation mode",
                trigger="device anomaly",
            )
        case VacationEmailSource.SETTINGS_TEST:
            return provenance_footer(
                subsystem="Automations → Vacation",
                trigger="test email",
            )
        case VacationEmailSource.LATCH:
            return provenance_footer(
                subsystem="Vacation mode",
                trigger="automatic",
            )
        case _:
            _LOGGER.warning(
                "[vacation] unknown VacationEmailSource for provenance: %s",
                source.value,
            )
            return provenance_footer(
                subsystem="Vacation mode",
                trigger=source.value,
            )


def _vacation_observed_distance_facts(
    *,
    cache_path: Path | None,
    user_ids: list[str],
) -> list[str]:
    """Return per-user observed distance-from-home lines when locations are known."""
    if cache_path is None:
        return []
    try:
        settings = load_settings_location()
        home = try_resolve_home_location(settings)
    except Exception:
        _LOGGER.exception(
            "[vacation] failed to resolve home for observed-distance email facts",
        )
        return []
    if home is None:
        return []
    try:
        locations = list_user_locations(cache_path)
    except Exception:
        _LOGGER.exception(
            "[vacation] failed to load user locations for observed-distance email facts",
        )
        return []
    lines: list[str] = []
    for user_id in user_ids:
        cleaned = user_id.strip()
        if cleaned == "":
            continue
        row = locations.get(cleaned)
        if row is None:
            lines.append(f"Observed distance ({cleaned}): unknown (no last location)")
            continue
        distance_m = _haversine_m(home.lat, home.lon, row.lat, row.lon)
        lines.append(
            f"Observed distance ({cleaned}): {_format_vacation_distance_m(distance_m)}",
        )
    return lines


_vacation_anomaly_debounce = _VacationAnomalyDebounce()
