"""users_inside_geofence_for_s with max_inside_s: a window for a fresh arrival, not for someone long home."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    GeofenceOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceForSCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_validation import build_roster_user_id_lookup

_TZ = ZoneInfo("America/New_York")
_NOW = datetime(2026, 6, 9, 19, 0, tzinfo=_TZ)
_SETTINGS = SettingsLocationOut(home_label="Home", lat=41.194072, lon=-73.8883254, timezone="America/New_York")


def _rule(*, max_inside_s: int | None) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    max_inside_s=max_inside_s,
                    min_inside_s=60,
                    user_ids=["kristen"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="arrival-window",
        label="Arrival window",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _ctx(*, inside_for_s: float) -> RuleEvaluationContext:
    names = {"kristen": "Kristen"}
    return RuleEvaluationContext(
        geofences=(
            GeofenceOut(
                center_lat=41.194072,
                center_lon=-73.888325,
                enabled=True,
                geofence_id="house",
                label="House",
                owntracks_rid=None,
                radius_m=250,
            ),
        ),
        now=_NOW,
        roster_user_id_lookup=build_roster_user_id_lookup(list(names)),
        sun=compute_rules_sun_out(_SETTINGS, now=_NOW),
        timezone=_TZ,
        user_display_names=names,
        user_locations={
            "kristen": UserLocationOut(
                accuracy_m=20,
                lat=41.1941,
                lon=-73.8883,
                fix_at="2026-06-09T23:00:00Z",
                reported_at="2026-06-09T23:00:00Z",
                source="owntracks",
            ),
        },
        geofence_inside_since={("kristen", "house"): _NOW.timestamp() - inside_for_s},
    )


def test_inside_within_the_window_is_met() -> None:
    row = evaluate_rule(_rule(max_inside_s=300), _ctx(inside_for_s=120.0)).conditions[0]
    assert row.met is True


def test_inside_longer_than_the_window_is_not_met_and_says_why() -> None:
    row = evaluate_rule(_rule(max_inside_s=300), _ctx(inside_for_s=400.0)).conditions[0]
    assert row.met is False
    assert row.detail == "Kristen inside 6 min 40 sec, past the 5 min arrival window"


def test_inside_for_hours_without_a_window_is_still_met() -> None:
    assert evaluate_rule(_rule(max_inside_s=None), _ctx(inside_for_s=4 * 3600.0)).conditions[0].met is True


def test_inside_for_less_than_the_minimum_is_not_met() -> None:
    assert evaluate_rule(_rule(max_inside_s=300), _ctx(inside_for_s=30.0)).conditions[0].met is False


def test_presence_users_drop_out_once_past_the_window() -> None:
    assert presence_user_ids_for_rule(_rule(max_inside_s=300), _ctx(inside_for_s=120.0)) == ("kristen",)
    assert presence_user_ids_for_rule(_rule(max_inside_s=300), _ctx(inside_for_s=400.0)) == ()


@pytest.mark.parametrize("max_inside_s", [0, 30, 60])
def test_window_must_be_longer_than_the_minimum(max_inside_s: int) -> None:
    with pytest.raises(ValidationError):
        UsersInsideGeofenceForSCondition(
            type="users_inside_geofence_for_s",
            geofence_id="house",
            max_inside_s=max_inside_s,
            min_inside_s=60,
            user_ids=["kristen"],
        )


def test_existing_dwell_conditions_have_no_window() -> None:
    rule = RuleOut.model_validate(_rule(max_inside_s=None).model_dump(mode="json"))
    condition = rule.conditions.all[0]
    assert isinstance(condition, UsersInsideGeofenceForSCondition)
    assert condition.max_inside_s is None
