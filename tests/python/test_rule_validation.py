"""Tests for automation rule reference validation."""

from __future__ import annotations

from app.api.schemas import (
    RuleConditionsOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from unittest.mock import MagicMock, patch

from app.api.schemas import RuleDeviceActionOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.rule_validation import (
    RosterUserRow,
    RuleValidationContext,
    _device_action_issue,
    build_roster_name_hint_lookup,
    build_roster_user_id_lookup,
    resolve_roster_user_id,
    rule_references_user_id,
    validate_rule,
)
from app.rule_actions import RuleActionDispatchError


def _arrival_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="evening-arrival-home-lights",
        label="Evening arrival",
        min_location_accuracy_m=50,
        notification_email=None,
        notify_on_fire=False,
        trigger="edge_true",
    )


def test_resolve_roster_user_id_is_case_insensitive() -> None:
    lookup = build_roster_user_id_lookup(["Henrique", "kristen"])
    assert resolve_roster_user_id("henrique", lookup) == "Henrique"
    assert resolve_roster_user_id("KRISTEN", lookup) == "kristen"


def test_rule_references_user_id_is_case_insensitive() -> None:
    assert rule_references_user_id(["henrique"], "Henrique") is True
    assert rule_references_user_id(["Henrique"], "henrique") is True
    assert rule_references_user_id(["kristen"], "henrique") is False


def test_validate_rule_flags_unknown_user() -> None:
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["kristen"]),
        smtp_configured=True,
    )
    issues = validate_rule(_arrival_rule(), ctx)
    assert len(issues) == 1
    assert issues[0].kind == "unknown_user"
    assert issues[0].reference == "henrique"


def test_validate_rule_suggests_roster_user_id_from_display_name() -> None:
    roster_users = [
        RosterUserRow(
            display_name="Henrique",
            first_name="Henrique",
            user_id="hcma",
        ),
    ]
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup=build_roster_name_hint_lookup(roster_users),
        roster_user_id_lookup=build_roster_user_id_lookup(["hcma", "kristen"]),
        smtp_configured=True,
    )
    issues = validate_rule(_arrival_rule(), ctx)
    user_issues = [issue for issue in issues if issue.kind == "unknown_user"]
    assert len(user_issues) == 1
    assert 'Did you mean user_id "hcma"?' in user_issues[0].detail


def test_build_roster_name_hint_lookup_omits_ambiguous_names() -> None:
    roster_users = [
        RosterUserRow(
            display_name="Chris",
            first_name="Chris",
            user_id="chris-a",
        ),
        RosterUserRow(
            display_name="Chris",
            first_name="Chris",
            user_id="chris-b",
        ),
    ]
    hints = build_roster_name_hint_lookup(roster_users)
    assert hints == {}


def test_device_action_issue_preserves_ambiguous_device_detail() -> None:
    action = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_ON,
        device_id="Garage",
        family_id=DeviceFamilyId.KASA,
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with patch(
        "app.rule_validation.resolve_kasa_host_by_label",
        side_effect=RuleActionDispatchError("Ambiguous kasa device 'Garage'"),
    ):
        issue = _device_action_issue(ctx, action)
    assert issue is not None
    assert "Ambiguous" in issue.detail


def test_validate_rule_flags_unknown_geofence() -> None:
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"office"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["henrique"]),
        smtp_configured=True,
    )
    issues = validate_rule(_arrival_rule(), ctx)
    assert any(issue.kind == "unknown_geofence" for issue in issues)
