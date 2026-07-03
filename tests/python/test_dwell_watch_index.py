"""Unit tests for curated dwell watch indexing."""

from __future__ import annotations

from app.api.schemas import (
    RuleConditionsOut,
    RuleOut,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import RuleTrigger
from app.dwell_watch_index import DwellDirection, build_dwell_watch_index


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
