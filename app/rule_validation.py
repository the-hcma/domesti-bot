"""Cross-check automation rules against persisted roster, geofences, and devices."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.api.schemas import (
    AllConditionsCondition,
    AnyConditionsCondition,
    RuleConditionOut,
    RuleDeviceActionOut,
    RuleOut,
    RuleReferenceIssueOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId
from app.rule_actions import (
    RuleActionDispatchError,
    resolve_kasa_host_by_label,
    resolve_sonos_identifier_by_label,
    resolve_tailwind_identifier_by_label,
)

if TYPE_CHECKING:
    from app.domesti_bot_cli import DeviceManagersState


@dataclass(frozen=True)
class RosterUserRow:
    """Minimal roster fields used for rule reference validation."""

    display_name: str
    first_name: str
    user_id: str


@dataclass(frozen=True)
class RuleValidationContext:
    """Known ids from SQLite and optional live device managers."""

    device_state: DeviceManagersState | None
    geofence_ids: frozenset[str]
    roster_name_hint_lookup: dict[str, str]
    roster_user_id_lookup: dict[str, str]
    smtp_configured: bool


def build_roster_name_hint_lookup(users: list[RosterUserRow]) -> dict[str, str]:
    """Map lowercase display/first name to canonical roster ``user_id`` when unique."""
    candidates: dict[str, set[str]] = {}
    for user in users:
        for label in (user.display_name, user.first_name):
            key = label.strip().lower()
            if key == "":
                continue
            candidates.setdefault(key, set()).add(user.user_id)
    return {
        key: next(iter(user_ids))
        for key, user_ids in candidates.items()
        if len(user_ids) == 1
    }


def build_roster_user_id_lookup(roster_user_ids: list[str]) -> dict[str, str]:
    """Map lowercase ``user_id`` to the canonical roster value."""
    lookup: dict[str, str] = {}
    for user_id in roster_user_ids:
        trimmed = user_id.strip()
        if trimmed == "":
            continue
        lookup[trimmed.lower()] = trimmed
    return lookup


def collect_rule_geofence_ids(rule: RuleOut) -> set[str]:
    """Return every ``geofence_id`` referenced by ``rule``."""
    ids: set[str] = set()
    for condition in rule.conditions.all:
        _walk_geofence_ids(condition, ids)
    return ids


def collect_rule_user_ids(rule: RuleOut) -> set[str]:
    """Return every ``user_id`` referenced by ``rule``."""
    ids: set[str] = set()
    for condition in rule.conditions.all:
        _walk_user_ids(condition, ids)
    return ids


def resolve_roster_user_id(
    reference: str,
    roster_user_id_lookup: dict[str, str],
) -> str | None:
    """Resolve a rule ``user_id`` reference to the canonical roster id."""
    trimmed = reference.strip()
    if trimmed == "":
        return None
    return roster_user_id_lookup.get(trimmed.lower())


def rule_references_user_id(
    condition_user_ids: list[str],
    roster_user_id: str,
) -> bool:
    """Return whether ``roster_user_id`` is listed on a geofence condition."""
    needle = roster_user_id.lower()
    return any(user_id.strip().lower() == needle for user_id in condition_user_ids)


def validate_rule(
    rule: RuleOut,
    ctx: RuleValidationContext,
) -> list[RuleReferenceIssueOut]:
    """Return reference issues for one rule (empty when everything resolves)."""
    issues: list[RuleReferenceIssueOut] = []
    issues.extend(_validate_users(rule, ctx))
    issues.extend(_validate_geofences(rule, ctx))
    issues.extend(_validate_device_actions(rule, ctx))
    issues.extend(_validate_notification(rule, ctx))
    return issues


def validate_rules(
    rules: list[RuleOut],
    ctx: RuleValidationContext,
) -> dict[str, list[RuleReferenceIssueOut]]:
    """Validate every rule; keys are rule ids."""
    return {rule.id: validate_rule(rule, ctx) for rule in rules}


def _device_action_issue(
    ctx: RuleValidationContext,
    action: RuleDeviceActionOut,
) -> RuleReferenceIssueOut | None:
    reference = action.device_id.strip()
    if reference == "":
        return RuleReferenceIssueOut(
            detail=(
                f"Expected non-empty {action.family_id.value} device_id "
                "in device_actions"
            ),
            kind="unknown_device",
            reference=reference,
        )
    if ctx.device_state is None:
        return RuleReferenceIssueOut(
            detail=(
                f"Cannot verify {action.family_id.value} device "
                f'"{reference}" — device discovery is not ready yet'
            ),
            kind="discovery_pending",
            reference=reference,
        )
    try:
        if _device_action_resolves(ctx, action):
            return None
    except RuleActionDispatchError as exc:
        return RuleReferenceIssueOut(
            detail=str(exc),
            kind="unknown_device",
            reference=reference,
        )
    return RuleReferenceIssueOut(
        detail=(
            f'Unknown {action.family_id.value} device "{reference}" '
            "(not found in the current device list)."
        ),
        kind="unknown_device",
        reference=reference,
    )


def _device_action_resolves(
    ctx: RuleValidationContext,
    action: RuleDeviceActionOut,
) -> bool:
    state = ctx.device_state
    if state is None:
        return False
    match action.family_id:
        case DeviceFamilyId.KASA:
            return (
                resolve_kasa_host_by_label(state.kasa_mgr, action.device_id)
                is not None
            )
        case DeviceFamilyId.SONOS:
            return (
                resolve_sonos_identifier_by_label(
                    state.sonos_mgr,
                    action.device_id,
                )
                is not None
            )
        case DeviceFamilyId.TAILWIND:
            return (
                resolve_tailwind_identifier_by_label(
                    state.tailwind_mgr,
                    action.device_id,
                )
                is not None
            )
        case _:
            return False


def _unknown_user_issue(
    reference: str,
    ctx: RuleValidationContext,
) -> RuleReferenceIssueOut:
    suggestion = ctx.roster_name_hint_lookup.get(reference.strip().lower())
    if suggestion is not None and suggestion.lower() != reference.strip().lower():
        return RuleReferenceIssueOut(
            detail=(
                f'User "{reference}" is not in the automation user roster. '
                f'Did you mean user_id "{suggestion}"?'
            ),
            kind="unknown_user",
            reference=reference,
        )
    return RuleReferenceIssueOut(
        detail=(
            f'User "{reference}" is not in the automation user roster '
            "(sync users from My Tracks)."
        ),
        kind="unknown_user",
        reference=reference,
    )


def _validate_device_actions(
    rule: RuleOut,
    ctx: RuleValidationContext,
) -> list[RuleReferenceIssueOut]:
    issues: list[RuleReferenceIssueOut] = []
    for action in rule.device_actions:
        issue = _device_action_issue(ctx, action)
        if issue is not None:
            issues.append(issue)
    return issues


def _validate_geofences(
    rule: RuleOut,
    ctx: RuleValidationContext,
) -> list[RuleReferenceIssueOut]:
    issues: list[RuleReferenceIssueOut] = []
    for geofence_id in sorted(collect_rule_geofence_ids(rule)):
        if geofence_id not in ctx.geofence_ids:
            issues.append(
                RuleReferenceIssueOut(
                    detail=(
                        f'Geofence "{geofence_id}" is not defined '
                        "(add it under Automations → Geofences)."
                    ),
                    kind="unknown_geofence",
                    reference=geofence_id,
                ),
            )
    return issues


def _validate_notification(
    rule: RuleOut,
    ctx: RuleValidationContext,
) -> list[RuleReferenceIssueOut]:
    if not rule.notify_on_fire:
        return []
    recipient = (rule.notification_email or "").strip()
    issues: list[RuleReferenceIssueOut] = []
    if recipient == "":
        issues.append(
            RuleReferenceIssueOut(
                detail=(
                    f'Rule "{rule.id}" has notify_on_fire enabled but no '
                    "notification_email"
                ),
                kind="missing_notification_email",
                reference=rule.id,
            ),
        )
        return issues
    if not ctx.smtp_configured:
        issues.append(
            RuleReferenceIssueOut(
                detail=(
                    "SMTP is not configured; notification emails cannot be sent "
                    "(configure under Automations → Mail)."
                ),
                kind="missing_smtp",
                reference=recipient,
            ),
        )
    return issues


def _validate_users(
    rule: RuleOut,
    ctx: RuleValidationContext,
) -> list[RuleReferenceIssueOut]:
    issues: list[RuleReferenceIssueOut] = []
    for user_id in sorted(collect_rule_user_ids(rule)):
        if resolve_roster_user_id(user_id, ctx.roster_user_id_lookup) is None:
            issues.append(_unknown_user_issue(user_id, ctx))
    return issues


def _walk_geofence_ids(condition: RuleConditionOut, ids: set[str]) -> None:
    if isinstance(
        condition,
        (
            UsersInsideGeofenceCondition,
            UsersInsideGeofenceForSCondition,
            UsersOutsideGeofenceCondition,
        ),
    ):
        ids.add(condition.geofence_id)
        return
    if isinstance(condition, AllConditionsCondition):
        for child in condition.conditions:
            _walk_geofence_ids(child, ids)
        return
    if isinstance(condition, AnyConditionsCondition):
        for child in condition.conditions:
            _walk_geofence_ids(child, ids)


def _walk_user_ids(condition: RuleConditionOut, ids: set[str]) -> None:
    if isinstance(
        condition,
        (
            UsersInsideGeofenceCondition,
            UsersInsideGeofenceForSCondition,
            UsersOutsideGeofenceCondition,
        ),
    ):
        ids.update(user_id.strip() for user_id in condition.user_ids if user_id.strip())
        return
    if isinstance(condition, AllConditionsCondition):
        for child in condition.conditions:
            _walk_user_ids(child, ids)
        return
    if isinstance(condition, AnyConditionsCondition):
        for child in condition.conditions:
            _walk_user_ids(child, ids)
