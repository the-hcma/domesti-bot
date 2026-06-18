"""Dispatch automation rule device actions and notification emails."""

from __future__ import annotations

import logging
from email.message import EmailMessage
from html import escape
from pathlib import Path

from app.api.schemas import RuleDeviceActionOut, RuleOut
from app.api.ui_state import (
    find_kasa_by_host,
    find_sonos_by_identifier,
    find_tailwind_by_identifier,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.smtp_service import SmtpConnectionParams, smtp_friendly_error
from app.smtp_store import load_smtp_config, resolve_password_for_send, smtp_send_ready
from app.sonos_device_manager import SonosDeviceManager

_LOGGER = logging.getLogger(__name__)


class RuleActionDispatchError(Exception):
    """Raised when a single rule device action cannot be dispatched."""


async def dispatch_device_action(
    state: DeviceManagersState,
    action: RuleDeviceActionOut,
) -> None:
    """Run one device action using the same code paths as the tile UI."""
    match action.family_id:
        case DeviceFamilyId.KASA:
            await _dispatch_kasa_action(state.kasa_mgr, action)
        case DeviceFamilyId.TAILWIND:
            await _dispatch_tailwind_action(state.tailwind_mgr, action)
        case DeviceFamilyId.SONOS:
            await _dispatch_sonos_action(state.sonos_mgr, action)
        case _:
            raise RuleActionDispatchError(
                "Expected supported device family, got "
                f"{action.family_id.display_name()!r}"
            )


async def dispatch_rule_device_actions(
    state: DeviceManagersState,
    actions: list[RuleDeviceActionOut],
) -> list[str]:
    """Run device actions sequentially; return human-readable errors."""
    errors: list[str] = []
    for action in actions:
        try:
            await dispatch_device_action(state, action)
        except RuleActionDispatchError as exc:
            errors.append(str(exc))
            _LOGGER.warning(
                "[rules] device action failed family=%s device=%s action=%s: %s",
                action.family_id,
                action.device_id,
                action.action,
                exc,
            )
    return errors


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
    return switch.is_on


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


def send_rule_notification_email(
    cache_path: Path,
    *,
    rule: RuleOut,
) -> None:
    """Send the rule notification email when ``notify_on_fire`` is enabled."""
    if not rule.notify_on_fire:
        return
    recipient = (rule.notification_email or "").strip()
    if recipient == "":
        raise RuleActionDispatchError(
            f"Rule {rule.id!r} has notify_on_fire but no notification_email"
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
    plain_body = (
        f'The automation rule "{rule.label}" ({rule.id}) just fired.\n\n'
        "Open Automations → Status in domesti-bot for live condition details."
    )
    safe_label = escape(rule.label, quote=False)
    safe_id = escape(rule.id, quote=False)
    html_body = (
        f"<p>The automation rule <strong>{safe_label}</strong> "
        f"(<code>{safe_id}</code>) just fired.</p>"
        "<p>Open Automations → Status in domesti-bot for live condition details.</p>"
    )
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = params.from_address
    message["To"] = recipient
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    try:
        from app.smtp_service import deliver_email_message

        deliver_email_message(params, message)
    except Exception as exc:
        raise RuleActionDispatchError(
            smtp_friendly_error(exc, host=params.host)
        ) from exc
    _LOGGER.info("[rules] notification email sent for rule_id=%s to %s", rule.id, recipient)


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
