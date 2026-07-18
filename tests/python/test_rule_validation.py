"""Tests for automation rule reference validation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AfterSunsetCondition,
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersMinDistanceFromHomeMCondition,
    UsersOutsideGeofenceCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.rule_actions import RuleActionDispatchError
from app.rule_device_id import RULE_DEVICE_ID_DISPLAY_NAME_WARNING
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
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def test_geofence_condition_rejects_empty_user_ids() -> None:
    with pytest.raises(ValidationError):
        UsersInsideGeofenceCondition(
            type="users_inside_geofence",
            geofence_id="house",
            user_ids=[],
        )
    with pytest.raises(ValidationError):
        UsersInsideGeofenceForSCondition(
            type="users_inside_geofence_for_s",
            geofence_id="house",
            min_inside_s=600,
            user_ids=[],
        )
    with pytest.raises(ValidationError):
        UsersOutsideGeofenceCondition(
            type="users_outside_geofence",
            geofence_id="house",
            user_ids=[],
        )
    with pytest.raises(ValidationError):
        UsersMinDistanceFromHomeMCondition(
            type="users_min_distance_from_home_m",
            min_distance_m=80_000,
            user_ids=[],
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


def test_validate_rule_flags_geofence_edge_grace_disabled() -> None:
    rule = _arrival_rule().model_copy(update={"accuracy_edge_grace_s": 0})
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["henrique"]),
        smtp_configured=True,
    )
    issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "geofence_edge_grace_disabled"
    assert issues[0].reference == rule.id


def test_validate_rule_accepts_notify_on_fire_when_smtp_relay_ready() -> None:
    rule = _arrival_rule().model_copy(
        update={
            "notify_on_fire": True,
            "notification_emails": ["ops@hcma.info"],
        },
    )
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["henrique"]),
        smtp_configured=True,
    )
    issues = validate_rule(rule, ctx)
    assert not any(issue.kind == "missing_smtp" for issue in issues)


def test_validate_rule_flags_missing_notification_email() -> None:
    rule = _arrival_rule().model_copy(
        update={
            "notify_on_fire": True,
            "notification_emails": [],
        },
    )
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["henrique"]),
        smtp_configured=True,
    )
    issues = validate_rule(rule, ctx)
    assert any(issue.kind == "missing_notification_email" for issue in issues)


def test_validate_rule_flags_missing_smtp_when_auth_required() -> None:
    rule = _arrival_rule().model_copy(
        update={
            "notify_on_fire": True,
            "notification_emails": ["ops@hcma.info"],
        },
    )
    ctx = RuleValidationContext(
        device_state=None,
        geofence_ids=frozenset({"house"}),
        roster_name_hint_lookup={},
        roster_user_id_lookup=build_roster_user_id_lookup(["henrique"]),
        smtp_configured=False,
    )
    issues = validate_rule(rule, ctx)
    assert any(issue.kind == "missing_smtp" for issue in issues)


def test_rule_out_allows_astronomical_schedule_without_cron() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-15,
                    window_end="midnight",
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="astronomical-scheduled",
        label="Astronomical scheduled",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
    )
    assert rule.schedule_cron is None


def test_rule_out_requires_schedule_cron_for_scheduled_trigger() -> None:
    with pytest.raises(ValidationError, match="schedule_cron"):
        RuleOut(
            conditions=RuleConditionsOut(all=[]),
            cooldown_s=60,
            device_actions=[],
            enabled=True,
            id="bad-scheduled",
            label="Bad scheduled",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.SCHEDULED],
        )


def test_rule_out_rejects_schedule_cron_on_edge_true_trigger() -> None:
    with pytest.raises(
        ValidationError,
        match="only allowed when triggers includes scheduled",
    ):
        RuleOut(
            conditions=RuleConditionsOut(all=[]),
            cooldown_s=60,
            device_actions=[],
            enabled=True,
            id="bad-cron",
            label="Bad cron",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            schedule_cron="* * * * *",
            triggers=[RuleTrigger.EDGE_TRUE],
        )


def test_rule_out_rejects_invalid_schedule_cron() -> None:
    with pytest.raises(ValidationError, match="5-field cron"):
        RuleOut(
            conditions=RuleConditionsOut(all=[]),
            cooldown_s=60,
            device_actions=[],
            enabled=True,
            id="bad-cron-expr",
            label="Bad cron expr",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            schedule_cron="not-a-cron",
            triggers=[RuleTrigger.SCHEDULED],
        )


def test_rule_out_normalizes_schedule_cron_whitespace() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="trimmed-cron",
        label="Trimmed cron",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="  */15 * * * *  ",
        triggers=[RuleTrigger.SCHEDULED],
    )
    assert rule.schedule_cron == "*/15 * * * *"


def test_rule_out_coerces_whitespace_only_schedule_cron_to_none_for_edge_true() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="blank-cron",
        label="Blank cron",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="   ",
        triggers=[RuleTrigger.EDGE_TRUE],
    )
    assert rule.schedule_cron is None


def test_rule_out_accepts_device_state_trigger_with_device_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OPEN,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Left",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="garage-open",
        label="Garage open",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
    )
    assert rule.triggers == [RuleTrigger.DEVICE_STATE]
    assert rule.schedule_cron is None


def test_rule_out_accepts_dwell_satisfied_trigger_with_dwell_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="away-dwell",
        label="Away dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=True,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )
    assert rule.triggers == [RuleTrigger.DWELL_SATISFIED]
    assert rule.schedule_cron is None


def test_rule_out_requires_device_ref_for_device_state_trigger() -> None:
    with pytest.raises(ValidationError, match="device_state rules must reference"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    UsersOutsideGeofenceCondition(
                        type="users_outside_geofence",
                        geofence_id="house",
                        user_ids=["henrique"],
                    ),
                ],
            ),
            cooldown_s=300,
            device_actions=[],
            enabled=True,
            id="away-no-device",
            label="Away no device",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.DEVICE_STATE],
        )


def test_rule_out_requires_dwell_condition_for_dwell_satisfied_trigger() -> None:
    with pytest.raises(ValidationError, match="dwell_satisfied rules must include"):
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    UsersOutsideGeofenceCondition(
                        type="users_outside_geofence",
                        geofence_id="house",
                        user_ids=["henrique"],
                    ),
                ],
            ),
            cooldown_s=300,
            device_actions=[],
            enabled=True,
            id="away-no-dwell",
            label="Away no dwell",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.DWELL_SATISFIED],
        )


def test_rule_out_coerces_null_accuracy_edge_grace_s_to_zero() -> None:
    rule = RuleOut.model_validate(
        {
            "accuracy_edge_grace_s": None,
            "conditions": {"all": []},
            "cooldown_s": 60,
            "device_actions": [],
            "enabled": True,
            "id": "legacy-grace-null",
            "label": "Legacy grace null",
            "min_location_accuracy_m": 50,
            "notification_emails": [],
            "notify_on_fire": False,
            "triggers": ["edge_true"],
        },
    )
    assert rule.accuracy_edge_grace_s == 0


def test_rule_out_rejects_legacy_trigger_field() -> None:
    with pytest.raises(ValidationError, match="legacy trigger field"):
        RuleOut.model_validate(
            {
                "conditions": {"all": []},
                "cooldown_s": 60,
                "device_actions": [],
                "enabled": True,
                "id": "legacy-trigger",
                "label": "Legacy trigger",
                "min_location_accuracy_m": 50,
                "notification_emails": [],
                "notify_on_fire": False,
                "trigger": "edge_true",
            },
        )


def test_devices_any_off_condition_rejects_empty_devices() -> None:
    with pytest.raises(ValidationError):
        DevicesAnyInStateCondition(type="devices_any_in_state", state=DeviceConditionState.OFF, devices=[])


def test_rule_device_action_delay_s_rejects_negative() -> None:
    with pytest.raises(ValidationError):
        RuleDeviceActionOut(
            action=RuleDeviceActionType.TURN_ON,
            delay_s=-1,
            device_id="Tuner",
            family_id=DeviceFamilyId.KASA,
        )


def test_rule_device_action_delay_s_rejects_above_24h() -> None:
    with pytest.raises(ValidationError):
        RuleDeviceActionOut(
            action=RuleDeviceActionType.TURN_ON,
            delay_s=86_401,
            device_id="Tuner",
            family_id=DeviceFamilyId.KASA,
        )


def test_rule_device_action_delay_s_accepts_zero_and_max() -> None:
    zero = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_OFF,
        delay_s=0,
        device_id="Tuner",
        family_id=DeviceFamilyId.KASA,
    )
    capped = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_ON,
        delay_s=86_400,
        device_id="Tuner",
        family_id=DeviceFamilyId.KASA,
    )
    assert zero.delay_s == 0
    assert capped.delay_s == 86_400


def test_devices_any_on_condition_rejects_empty_devices() -> None:
    with pytest.raises(ValidationError):
        DevicesAnyInStateCondition(type="devices_any_in_state", state=DeviceConditionState.ON, devices=[])


def test_validate_rule_accepts_kasa_devices_any_off_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OFF,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:01",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="kasa-any-off",
        label="Kasa any off",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
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
        return_value="aa:bb:cc:dd:ee:01",
    ):
        issues = validate_rule(rule, ctx)
    assert issues == []


def test_validate_rule_warns_when_kasa_device_id_is_display_name() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OFF,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Basement lamp",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="kasa-label",
        label="Kasa label",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
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
        return_value="aa:bb:cc:dd:ee:01",
    ):
        issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "non_canonical_device_id"
    assert issues[0].detail == RULE_DEVICE_ID_DISPLAY_NAME_WARNING.format(
        device_id="Basement lamp",
    )


def test_validate_rule_flags_unknown_kasa_devices_any_off_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OFF,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Basement lamp",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="kasa-any-off-missing",
        label="Kasa any off missing",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
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
        return_value=None,
    ):
        issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "unknown_device"
    assert 'Unknown kasa device "Basement lamp"' in issues[0].detail


def test_validate_rule_accepts_tailwind_devices_any_open_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OPEN,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:ff:door-1",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="tailwind-any-open",
        label="Tailwind any open",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with patch(
        "app.rule_validation.resolve_tailwind_identifier_by_label",
        return_value="aa:bb:cc:dd:ee:ff:door-1",
    ):
        issues = validate_rule(rule, ctx)
    assert issues == []


def test_validate_rule_flags_unknown_tailwind_devices_any_open_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OPEN,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Henrique's side",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="tailwind-any-open-missing",
        label="Tailwind any open missing",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with patch(
        "app.rule_validation.resolve_tailwind_identifier_by_label",
        return_value=None,
    ):
        issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "unknown_device"
    assert 'Unknown tailwind device "Henrique\'s side"' in issues[0].detail


def test_validate_rule_accepts_sonos_device_condition_when_zone_exists() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="aa:bb:cc:dd:ee:10",
                            family_id=DeviceFamilyId.SONOS,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="sonos-condition",
        label="Sonos condition",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with patch(
        "app.rule_validation.resolve_sonos_identifier_by_label",
        return_value="aa:bb:cc:dd:ee:10",
    ):
        issues = validate_rule(rule, ctx)
    assert issues == []


def test_validate_rule_flags_androidtv_device_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Living Room TV",
                            family_id=DeviceFamilyId.ANDROIDTV,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="cast-condition",
        label="Cast condition",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    issues = validate_rule(rule, ctx)
    assert len(issues) == 2
    assert all(issue.kind == "unknown_device" for issue in issues)
    details = " ".join(issue.detail for issue in issues)
    assert 'Unknown androidtv device "Living Room TV"' in details
    assert "cannot report state on" in details


def test_validate_rule_flags_unknown_sonos_device_condition() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Kitchen",
                            family_id=DeviceFamilyId.SONOS,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="sonos-condition",
        label="Sonos condition",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    with patch(
        "app.rule_validation.resolve_sonos_identifier_by_label",
        return_value=None,
    ):
        issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "unknown_device"
    assert 'Unknown sonos device "Kitchen"' in issues[0].detail


def test_validate_rule_flags_blank_device_condition_ref() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="   ",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        id="blank-device-condition",
        label="Blank device condition",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/15 * * * *",
    )
    ctx = RuleValidationContext(
        device_state=MagicMock(),
        geofence_ids=frozenset(),
        roster_name_hint_lookup={},
        roster_user_id_lookup={},
        smtp_configured=True,
    )
    issues = validate_rule(rule, ctx)
    assert len(issues) == 1
    assert issues[0].kind == "unknown_device"
    assert "conditions" in issues[0].detail


def test_rule_out_accepts_fire_once_per_local_day_on_edge_true_trigger() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=True,
        id="daily-cap-edge",
        label="Daily cap edge",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )
    assert rule.fire_once_per_local_day is True


def test_rule_out_accepts_fire_once_per_local_day_on_scheduled_trigger() -> None:
    rule = RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=60,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=True,
        id="daily-cap",
        label="Daily cap",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="*/15 * * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )
    assert rule.fire_once_per_local_day is True
