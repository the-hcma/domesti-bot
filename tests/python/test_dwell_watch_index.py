"""Unit tests for curated dwell watch indexing."""

from __future__ import annotations

from app.api.schemas import (
    DevicesAnyInStateForSCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.dwell_watch_index import (
    DwellDirection,
    build_device_dwell_watch_index,
    build_dwell_watch_index,
)


def test_build_device_dwell_watch_index_indexes_open_door_dwell() -> None:
    rules = [
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    DevicesAnyInStateForSCondition(
                        type="devices_any_in_state_for_s",
                        devices=[
                            RuleConditionDeviceRefOut(
                                device_id="Left",
                                family_id=DeviceFamilyId.TAILWIND,
                            ),
                        ],
                        min_duration_s=1200,
                        state=DeviceConditionState.OPEN,
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="away-garage-open-alert",
            label="Away garage",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.DWELL_SATISFIED],
        ),
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    DevicesAnyInStateForSCondition(
                        type="devices_any_in_state_for_s",
                        devices=[
                            RuleConditionDeviceRefOut(
                                device_id="Left",
                                family_id=DeviceFamilyId.TAILWIND,
                            ),
                        ],
                        min_duration_s=1200,
                        state=DeviceConditionState.OPEN,
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="scheduled-ignored",
            label="Ignored",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.SCHEDULED],
            schedule_cron="*/10 * * * *",
        ),
    ]

    index = build_device_dwell_watch_index(rules)

    assert len(index.watches) == 1
    watch = index.watches[0]
    assert watch.device_id == "Left"
    assert watch.family_id == DeviceFamilyId.TAILWIND
    assert watch.min_duration_s == 1200
    assert watch.rule_ids == frozenset({"away-garage-open-alert"})
    assert watch.state == DeviceConditionState.OPEN


def test_build_dwell_watch_index_ignores_non_dwell_satisfied_rules() -> None:
    rules = [
        RuleOut(
            conditions=RuleConditionsOut(
                all=[
                    UsersInsideGeofenceForSCondition(
                        type="users_inside_geofence_for_s",
                        geofence_id="house",
                        min_inside_s=300,
                        user_ids=["hcma"],
                    ),
                ],
            ),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="scheduled-only",
            label="Scheduled",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            schedule_cron="*/10 * * * *",
            triggers=[RuleTrigger.SCHEDULED],
        ),
    ]

    assert build_dwell_watch_index(rules).watches == ()


def test_build_dwell_watch_index_merges_rules_for_same_tuple() -> None:
    condition = UsersOutsideGeofenceForSCondition(
        type="users_outside_geofence_for_s",
        geofence_id="house",
        min_outside_s=600,
        user_ids=["hcma"],
    )
    rules = [
        RuleOut(
            conditions=RuleConditionsOut(all=[condition]),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="rule-a",
            label="A",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.DWELL_SATISFIED],
        ),
        RuleOut(
            conditions=RuleConditionsOut(all=[condition]),
            cooldown_s=0,
            device_actions=[],
            enabled=True,
            id="rule-b",
            label="B",
            min_location_accuracy_m=50,
            notification_emails=[],
            notify_on_fire=False,
            triggers=[RuleTrigger.DWELL_SATISFIED],
        ),
    ]

    index = build_dwell_watch_index(rules)

    assert len(index.watches) == 1
    watch = index.watches[0]
    assert watch.direction == DwellDirection.OUTSIDE
    assert watch.geofence_id == "house"
    assert watch.min_s == 600
    assert watch.rule_user_id == "hcma"
    assert watch.rule_ids == frozenset({"rule-a", "rule-b"})
