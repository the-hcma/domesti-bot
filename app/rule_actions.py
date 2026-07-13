"""Dispatch automation rule device actions and notification emails."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage
from pathlib import Path
from typing import Literal

from kasa.exceptions import _ConnectionError

from app.api.schemas import RuleDeviceActionOut, RuleOut, normalized_rule_notification_emails
from app.api.ui_state import (
    find_kasa_by_host,
    find_sonos_by_identifier,
    find_tailwind_by_identifier,
    find_vizio_by_id,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.operator_alerts import operator_alert_store
from app.rule_device_action_outcome import RuleDeviceActionOutcome
from app.rule_engine import expected_state_for_action_type
from app.rule_notification import build_rule_notification_bodies
from app.smtp_service import SmtpConnectionParams, SmtpDeliveryResult, smtp_friendly_error
from app.smtp_store import load_smtp_config, resolve_password_for_send, smtp_send_ready
from app.sonos_device_manager import SonosDeviceManager, SonosTransitionUnavailableError
from app.vizio_device_manager import VizioDeviceManager

_LOGGER = logging.getLogger(__name__)


class RuleActionDispatchError(Exception):
    """Raised when a single rule device action cannot be dispatched."""


@dataclass(frozen=True)
class RuleDeviceDispatchResult:
    """Outcome of dispatching all device actions for one rule fire."""

    action_outcomes: tuple[RuleDeviceActionOutcome, ...]
    errors: tuple[str, ...]
    probable_successes: tuple[str, ...]

    @classmethod
    def empty(cls) -> RuleDeviceDispatchResult:
        return cls(action_outcomes=(), errors=(), probable_successes=())


@dataclass(frozen=True)
class RuleNotificationEmailOutcome:
    """Outcome of attempting to send a rule notification email."""

    kind: Literal["disabled", "sent"]
    recipients: tuple[str, ...] | None = None
    delivery: SmtpDeliveryResult | None = None

    @classmethod
    def disabled(cls) -> RuleNotificationEmailOutcome:
        return cls(kind="disabled", recipients=None)

    def format_for_log(self, *, redact_recipients: bool = False) -> str:
        if self.kind == "disabled":
            return "disabled"
        if self.kind == "sent":
            if not self.recipients:
                raise AssertionError("sent outcome requires recipients")
            if redact_recipients:
                parts = ["sent"]
                if self.delivery is None:
                    parts.append(f"recipient_count={len(self.recipients)}")
            else:
                parts = [f"sent to={','.join(self.recipients)}"]
            if self.delivery is not None:
                parts.append(self.delivery.format_for_log(redact_recipients=redact_recipients))
            return " ".join(parts)
        raise AssertionError(f"Unexpected notification email outcome kind {self.kind!r}")

    @classmethod
    def sent_to(
        cls,
        recipients: list[str],
        *,
        delivery: SmtpDeliveryResult | None = None,
    ) -> RuleNotificationEmailOutcome:
        return cls(kind="sent", recipients=tuple(recipients), delivery=delivery)


def _device_action_failure_is_probable(
    action: RuleDeviceActionOut,
    exc: BaseException,
) -> bool:
    """Return whether a post-command failure on off/pause/close may still have succeeded."""
    match action.action:
        case RuleDeviceActionType.CLOSE | RuleDeviceActionType.PAUSE | RuleDeviceActionType.TURN_OFF:
            return isinstance(
                exc,
                (OSError, SonosTransitionUnavailableError, _ConnectionError),
            )
        case _:
            return False


async def _dispatch_kasa_action(
    mgr: KasaDeviceManager,
    action: RuleDeviceActionOut,
) -> None:
    host = resolve_kasa_host_by_label(mgr, action.device_id)
    if host is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.KASA.display_name()} device: {action.device_id!r}"
        )
    device = find_kasa_by_host(mgr, host)
    if device is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.KASA.display_name()} device: {action.device_id!r}"
        )
    match action.action:
        case RuleDeviceActionType.TURN_ON:
            await device.turn_on()
        case RuleDeviceActionType.TURN_OFF:
            await device.turn_off()
        case _:
            raise RuleActionDispatchError(
                f"Expected {DeviceFamilyId.KASA.display_name()} action turn on or "
                f"turn off, got {action.action.display_label()!r}"
            )


async def _dispatch_sonos_action(
    mgr: SonosDeviceManager | None,
    action: RuleDeviceActionOut,
) -> None:
    if mgr is None:
        raise RuleActionDispatchError(
            f"{DeviceFamilyId.SONOS.display_name()} manager is not configured on this server"
        )
    identifier = resolve_sonos_identifier_by_label(mgr, action.device_id)
    if identifier is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.SONOS.display_name()} zone: {action.device_id!r}"
        )
    zone = find_sonos_by_identifier(mgr, identifier)
    if zone is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.SONOS.display_name()} zone: {action.device_id!r}"
        )
    match action.action:
        case RuleDeviceActionType.PAUSE:
            await zone.pause()
        case RuleDeviceActionType.RESUME:
            await zone.resume()
        case _:
            raise RuleActionDispatchError(
                f"Expected {DeviceFamilyId.SONOS.display_name()} action pause or "
                f"resume, got {action.action.display_label()!r}"
            )


async def _dispatch_tailwind_action(
    mgr: GotailwindDeviceManager | None,
    action: RuleDeviceActionOut,
) -> None:
    if mgr is None:
        raise RuleActionDispatchError(
            f"{DeviceFamilyId.TAILWIND.display_name()} manager is not configured on "
            "this server"
        )
    identifier = resolve_tailwind_identifier_by_label(mgr, action.device_id)
    if identifier is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.TAILWIND.display_name()} door: {action.device_id!r}"
        )
    door = find_tailwind_by_identifier(mgr, identifier)
    if door is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.TAILWIND.display_name()} door: {action.device_id!r}"
        )
    match action.action:
        case RuleDeviceActionType.OPEN:
            await door.open()
        case RuleDeviceActionType.CLOSE:
            await door.close()
        case _:
            raise RuleActionDispatchError(
                f"Expected {DeviceFamilyId.TAILWIND.display_name()} action open or "
                f"close, got {action.action.display_label()!r}"
            )


async def _dispatch_vizio_action(
    mgr: VizioDeviceManager | None,
    action: RuleDeviceActionOut,
) -> None:
    if mgr is None:
        raise RuleActionDispatchError(
            f"{DeviceFamilyId.VIZIO.display_name()} manager is not configured on this server"
        )
    identifier = resolve_vizio_identifier_by_label(mgr, action.device_id)
    if identifier is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.VIZIO.display_name()} TV: {action.device_id!r}"
        )
    tv = find_vizio_by_id(mgr, identifier)
    if tv is None:
        raise RuleActionDispatchError(
            f"Unknown {DeviceFamilyId.VIZIO.display_name()} TV: {action.device_id!r}"
        )
    match action.action:
        case RuleDeviceActionType.TURN_ON:
            await tv.turn_on()
        case RuleDeviceActionType.TURN_OFF:
            await tv.turn_off()
        case _:
            raise RuleActionDispatchError(
                f"Expected {DeviceFamilyId.VIZIO.display_name()} action turn on or "
                f"turn off, got {action.action.display_label()!r}"
            )


def after_state_for_action(
    action: RuleDeviceActionOut,
    *,
    before_state: str | None,
    observed_after: str | None,
    succeeded: bool,
) -> str | None:
    """Return the best available post-action state label for notification email."""
    if not succeeded:
        if observed_after is not None:
            return observed_after
        return before_state
    expected = expected_state_after_action(action)
    if observed_after is None:
        return expected
    if observed_after != before_state:
        return observed_after
    return expected


def cached_kasa_is_on(state: DeviceManagersState, device_id: str) -> bool | None:
    """Return cached on/off for a Kasa label or host, or ``None`` when not found."""
    try:
        host = resolve_kasa_host_by_label(state.kasa_mgr, device_id)
    except RuleActionDispatchError:
        return None
    if host is None:
        return None
    switch = find_kasa_by_host(state.kasa_mgr, host)
    if switch is None:
        return None
    is_on = getattr(switch, "is_on", None)
    if is_on is None:
        return None
    return bool(is_on)


def cached_sonos_is_playing(state: DeviceManagersState, device_id: str) -> bool | None:
    """Return cached playback state for a Sonos zone label, or ``None`` when not found."""
    try:
        identifier = resolve_sonos_identifier_by_label(state.sonos_mgr, device_id)
    except RuleActionDispatchError:
        return None
    if identifier is None:
        return None
    mgr = state.sonos_mgr
    if mgr is None:
        return None
    zone = find_sonos_by_identifier(mgr, identifier)
    if zone is None:
        return None
    is_playing = getattr(zone, "is_playing", None)
    if is_playing is None:
        return None
    return bool(is_playing)


def cached_tailwind_is_open(state: DeviceManagersState, device_id: str) -> bool | None:
    """Return cached open/closed for a Tailwind door label, or ``None`` when not found."""
    try:
        identifier = resolve_tailwind_identifier_by_label(state.tailwind_mgr, device_id)
    except RuleActionDispatchError:
        return None
    if identifier is None:
        return None
    mgr = state.tailwind_mgr
    if mgr is None:
        return None
    door = find_tailwind_by_identifier(mgr, identifier)
    if door is None:
        return None
    is_open = getattr(door, "is_open", None)
    if is_open is None:
        return None
    return bool(is_open)


def cached_vizio_is_on(state: DeviceManagersState, device_id: str) -> bool | None:
    """Return cached on/off for a Vizio TV label, or ``None`` when not found or unknown."""
    try:
        identifier = resolve_vizio_identifier_by_label(state.vizio_mgr, device_id)
    except RuleActionDispatchError:
        return None
    if identifier is None:
        return None
    mgr = state.vizio_mgr
    if mgr is None:
        return None
    tv = find_vizio_by_id(mgr, identifier)
    if tv is None:
        return None
    if tv.ui_power_state() == "unknown":
        return None
    return tv.ui_power_state() == "on"


async def dispatch_device_action(
    state: DeviceManagersState,
    action: RuleDeviceActionOut,
) -> None:
    """Run one device action using the same code paths as the tile UI."""
    match action.family_id:
        case DeviceFamilyId.KASA:
            await _dispatch_kasa_action(state.kasa_mgr, action)
        case DeviceFamilyId.SONOS:
            await _dispatch_sonos_action(state.sonos_mgr, action)
        case DeviceFamilyId.TAILWIND:
            await _dispatch_tailwind_action(state.tailwind_mgr, action)
        case DeviceFamilyId.VIZIO:
            await _dispatch_vizio_action(state.vizio_mgr, action)
        case _:
            raise RuleActionDispatchError(
                "Expected supported device family, got "
                f"{action.family_id.display_name()!r}"
            )


async def dispatch_rule_device_actions(
    state: DeviceManagersState,
    actions: list[RuleDeviceActionOut],
) -> RuleDeviceDispatchResult:
    """Run device actions sequentially; collect outcomes and errors."""
    errors: list[str] = []
    probable_successes: list[str] = []
    action_outcomes: list[RuleDeviceActionOutcome] = []
    for action in actions:
        before_state = snapshot_device_action_state(state, action)
        try:
            await dispatch_device_action(state, action)
        except RuleActionDispatchError as exc:
            errors.append(str(exc))
            action_outcomes.append(
                RuleDeviceActionOutcome(
                    action=action.action,
                    after_state=after_state_for_action(
                        action,
                        before_state=before_state,
                        observed_after=snapshot_device_action_state(state, action),
                        succeeded=False,
                    ),
                    before_state=before_state,
                    device_id=action.device_id,
                    error=str(exc),
                    family_id=action.family_id,
                    probable=False,
                    succeeded=False,
                ),
            )
            _LOGGER.warning(
                "[rules] device action failed family=%s device=%s action=%s: %s",
                action.family_id,
                action.device_id,
                action.action,
                exc,
            )
            continue
        except Exception as exc:
            message = (
                f"{action.family_id.display_name()} device {action.device_id!r} "
                f"{action.action.display_label()} failed: {exc}"
            )
            probable = _device_action_failure_is_probable(action, exc)
            observed_after = snapshot_device_action_state(state, action)
            if probable and observed_after == before_state:
                observed_after = None
            action_outcomes.append(
                RuleDeviceActionOutcome(
                    action=action.action,
                    after_state=after_state_for_action(
                        action,
                        before_state=before_state,
                        observed_after=observed_after,
                        succeeded=probable,
                    ),
                    before_state=before_state,
                    device_id=action.device_id,
                    error=message if not probable else None,
                    family_id=action.family_id,
                    probable=probable,
                    succeeded=probable,
                ),
            )
            if probable:
                probable_text = f"{message} (probable)"
                probable_successes.append(probable_text)
                _LOGGER.info(
                    "[rules] device action probable success family=%s device=%s "
                    "action=%s: %s",
                    action.family_id,
                    action.device_id,
                    action.action,
                    probable_text,
                )
            else:
                errors.append(message)
                _LOGGER.warning(
                    "[rules] device action failed family=%s device=%s action=%s: %s",
                    action.family_id,
                    action.device_id,
                    action.action,
                    message,
                )
            continue
        observed_after = snapshot_device_action_state(state, action)
        action_outcomes.append(
            RuleDeviceActionOutcome(
                action=action.action,
                after_state=after_state_for_action(
                    action,
                    before_state=before_state,
                    observed_after=observed_after,
                    succeeded=True,
                ),
                before_state=before_state,
                device_id=action.device_id,
                error=None,
                family_id=action.family_id,
                probable=False,
                succeeded=True,
            ),
        )
    return RuleDeviceDispatchResult(
        action_outcomes=tuple(action_outcomes),
        errors=tuple(errors),
        probable_successes=tuple(probable_successes),
    )


def expected_state_after_action(action: RuleDeviceActionOut) -> DeviceConditionState:
    """Return the nominal end state after a successful device action."""
    return expected_state_for_action_type(action.action)


def resolve_kasa_host_by_label(mgr: KasaDeviceManager, device_id: str) -> str | None:
    """Resolve a Kasa tile label (or host) to the canonical LAN host."""
    needle = device_id.strip()
    if not needle:
        return None
    if find_kasa_by_host(mgr, needle) is not None:
        return needle
    lower_needle = needle.lower()
    matches: list[str] = []
    for kd in mgr.switches:
        host = (kd._kDevice.host or "").strip()
        if not host:
            continue
        candidates = {host.lower(), kd.identifier.lower(), kd.preferred_label.lower()}
        if lower_needle in candidates:
            matches.append(host)
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise RuleActionDispatchError(
            f"Ambiguous {DeviceFamilyId.KASA.display_name()} device {device_id!r}; "
            f"matches: {', '.join(unique)}"
        )
    return None


def resolve_sonos_identifier_by_label(
    mgr: SonosDeviceManager | None,
    device_id: str,
) -> str | None:
    """Resolve a Sonos zone label to its ``RINCON_…`` identifier."""
    if mgr is None:
        return None
    needle = device_id.strip()
    if not needle:
        return None
    if find_sonos_by_identifier(mgr, needle) is not None:
        return needle
    lower_needle = needle.lower()
    matches: list[str] = []
    for zone in mgr.players:
        candidates = {zone.identifier.lower(), zone.preferred_label.lower()}
        if lower_needle in candidates:
            matches.append(zone.identifier)
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise RuleActionDispatchError(
            f"Ambiguous {DeviceFamilyId.SONOS.display_name()} zone {device_id!r}; "
            f"matches: {', '.join(unique)}"
        )
    return None


def resolve_tailwind_identifier_by_label(
    mgr: GotailwindDeviceManager | None,
    device_id: str,
) -> str | None:
    """Resolve a Tailwind door label to its canonical identifier."""
    if mgr is None:
        return None
    needle = device_id.strip()
    if not needle:
        return None
    if find_tailwind_by_identifier(mgr, needle) is not None:
        return needle
    lower_needle = needle.lower()
    matches: list[str] = []
    for door in mgr.doors:
        candidates = {door.identifier.lower(), door.preferred_label.lower()}
        if lower_needle in candidates:
            matches.append(door.identifier)
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise RuleActionDispatchError(
            f"Ambiguous {DeviceFamilyId.TAILWIND.display_name()} door {device_id!r}; "
            f"matches: {', '.join(unique)}"
        )
    return None


def resolve_vizio_identifier_by_label(
    mgr: VizioDeviceManager | None,
    device_id: str,
) -> str | None:
    """Resolve a Vizio TV label to its canonical identifier."""
    if mgr is None:
        return None
    needle = device_id.strip()
    if not needle:
        return None
    if find_vizio_by_id(mgr, needle) is not None:
        return needle
    lower_needle = needle.lower()
    matches: list[str] = []
    for tv in mgr.tvs:
        candidates = {tv.identifier.lower(), tv.preferred_label.lower()}
        if lower_needle in candidates:
            matches.append(tv.identifier)
    unique = sorted(set(matches))
    if len(unique) == 1:
        return unique[0]
    if len(unique) > 1:
        raise RuleActionDispatchError(
            f"Ambiguous {DeviceFamilyId.VIZIO.display_name()} TV {device_id!r}; "
            f"matches: {', '.join(unique)}"
        )
    return None


def send_rule_notification_email(
    cache_path: Path,
    *,
    device_action_outcomes: tuple[RuleDeviceActionOutcome, ...] = (),
    notification_detail: str | None = None,
    rule: RuleOut,
) -> RuleNotificationEmailOutcome:
    """Send the rule notification email when ``notify_on_fire`` is enabled."""
    if not rule.notify_on_fire:
        return RuleNotificationEmailOutcome.disabled()
    recipients = normalized_rule_notification_emails(rule)
    if not recipients:
        _LOGGER.error(
            "[rules] rule_id=%s notify_on_fire enabled but notification_emails is empty",
            rule.id,
        )
        raise RuleActionDispatchError(
            f"Rule {rule.id!r} has notify_on_fire but no notification_emails"
        )
    config = load_smtp_config(cache_path)
    if config is None or not smtp_send_ready(config):
        raise RuleActionDispatchError(
            "SMTP is not configured; cannot send rule notification email"
        )
    password = resolve_password_for_send(cache_path, draft_password=None, host=config.host)
    params = SmtpConnectionParams(
        from_address=config.from_address,
        host=config.host,
        mail_domain=config.mail_domain,
        password=password,
        port=config.port,
        username=config.username,
    )
    subject = f"domesti-bot rule fired: {rule.label}"
    plain_body, html_body = build_rule_notification_bodies(
        rule,
        cache_path=cache_path,
        device_action_outcomes=device_action_outcomes,
        notification_detail=notification_detail,
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = params.from_address
    message["To"] = ", ".join(recipients)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    try:
        from app.smtp_service import deliver_email_message

        delivery = deliver_email_message(params, message)
    except Exception as exc:
        friendly = smtp_friendly_error(exc, host=params.host)
        operator_alert_store.record_smtp_notification_failure(message=friendly)
        _LOGGER.error(
            "[rules] notification email failed for rule_id=%s recipient_count=%d host=%s:%s: %s",
            rule.id,
            len(recipients),
            params.host,
            params.port,
            friendly,
        )
        raise RuleActionDispatchError(friendly) from exc
    operator_alert_store.clear_smtp_notification_failure()
    _LOGGER.info(
        "[rules] notification email sent for rule_id=%s %s",
        rule.id,
        delivery.format_for_log(redact_recipients=True),
    )
    return RuleNotificationEmailOutcome.sent_to(recipients, delivery=delivery)


def snapshot_device_action_state(
    state: DeviceManagersState,
    action: RuleDeviceActionOut,
) -> DeviceConditionState | None:
    """Return a human-readable cached device state label before/after dispatch."""
    match action.family_id:
        case DeviceFamilyId.KASA | DeviceFamilyId.VIZIO:
            is_on = cached_kasa_is_on(state, action.device_id) if (
                action.family_id == DeviceFamilyId.KASA
            ) else cached_vizio_is_on(state, action.device_id)
            if is_on is None:
                return None
            return DeviceConditionState.ON if is_on else DeviceConditionState.OFF
        case DeviceFamilyId.SONOS:
            is_playing = cached_sonos_is_playing(state, action.device_id)
            if is_playing is None:
                return None
            return (
                DeviceConditionState.PLAYING
                if is_playing
                else DeviceConditionState.PAUSED
            )
        case DeviceFamilyId.TAILWIND:
            is_open = cached_tailwind_is_open(state, action.device_id)
            if is_open is None:
                return None
            return DeviceConditionState.OPEN if is_open else DeviceConditionState.CLOSED
        case _:
            return None
