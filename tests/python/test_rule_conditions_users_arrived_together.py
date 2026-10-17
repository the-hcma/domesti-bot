"""users_arrived_together: both users inside a geofence, having entered within a short gap."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AnyConditionsCondition,
    GeofenceOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersArrivedTogetherCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_evaluator import _geofence_dwell_accuracy_limit_m, _max_dwell_min_inside_s
from app.rule_validation import build_roster_user_id_lookup

_TZ = ZoneInfo("America/New_York")
_NOW = datetime(2026, 6, 9, 19, 0, tzinfo=_TZ)
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)


def _condition(*, max_gap_s: int | None = None) -> UsersArrivedTogetherCondition:
    extra = {} if max_gap_s is None else {"max_gap_s": max_gap_s}
    return UsersArrivedTogetherCondition(
        type="users_arrived_together",
        geofence_id="house",
        user_ids=["henrique", "kristen"],
        **extra,
    )


def _rule(*, max_gap_s: int | None = None) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[_condition(max_gap_s=max_gap_s)]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="arrived-together",
        label="Arrived together",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def _geofence() -> GeofenceOut:
    return GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )


def _inside(*, accuracy_m: int = 20) -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=accuracy_m,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def _outside() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=20,
        lat=44.0,
        lon=-73.0,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def _fix(at: float, *, inside: bool, accuracy_m: int = 20) -> UserLocationOut:
    """A fix taken at ``at`` inside the geofence, or about 400 km away from it."""
    stamp = datetime.fromtimestamp(at, tz=ZoneInfo("UTC")).isoformat().replace("+00:00", "Z")
    return UserLocationOut(
        accuracy_m=accuracy_m,
        lat=41.1941 if inside else 44.0,
        lon=-73.8883 if inside else -73.0,
        fix_at=stamp,
        reported_at=stamp,
        source="owntracks",
    )


def _arrival_history(arrived_at: float, *, accuracy_m: int = 20) -> tuple[UserLocationOut, ...]:
    """Newest first: outside a second before ``arrived_at``, inside from then until now."""
    now = _NOW.timestamp()
    inside = [_fix(at, inside=True, accuracy_m=accuracy_m) for at in (now, (arrived_at + now) / 2, arrived_at)]
    return (*inside, _fix(arrived_at - 1.0, inside=False))


def _ctx(
    *,
    henrique_gap_s: float | None,
    kristen_since: float | None = None,
    geofences: tuple[GeofenceOut, ...] | None = None,
    user_locations: dict[str, UserLocationOut] | None = None,
    histories: dict[str, tuple[UserLocationOut, ...]] | None = None,
) -> RuleEvaluationContext:
    """Kristen arrived at ``kristen_since`` (default 5 min ago); Henrique ``henrique_gap_s`` after her."""
    kristen_at = _NOW.timestamp() - 300.0 if kristen_since is None else kristen_since
    built: dict[str, tuple[UserLocationOut, ...]] = {"kristen": _arrival_history(kristen_at)}
    if henrique_gap_s is not None:
        built["henrique"] = _arrival_history(kristen_at + henrique_gap_s)
    else:
        built["henrique"] = (_fix(_NOW.timestamp(), inside=True),)
    names = {"henrique": "Henrique", "kristen": "Kristen"}
    return RuleEvaluationContext(
        geofences=(_geofence(),) if geofences is None else geofences,
        now=_NOW,
        roster_user_id_lookup=build_roster_user_id_lookup(list(names)),
        sun=compute_rules_sun_out(_SETTINGS, now=_NOW),
        timezone=_TZ,
        user_display_names=names,
        user_locations=({"henrique": _inside(), "kristen": _inside()} if user_locations is None else user_locations),
        user_location_history=built if histories is None else histories,
    )


def test_two_users_entering_seconds_apart_arrived_together() -> None:
    result = evaluate_rule(_rule(), _ctx(henrique_gap_s=30.0))
    row = result.conditions[0]
    assert row.met is True
    assert row.detail == "Henrique and Kristen arrived at House 30 sec apart (limit 1 min)"
    assert row.label == "Arrived at House within 1 min of each other (Henrique and Kristen)"


def test_entering_exactly_at_the_limit_still_counts_as_together() -> None:
    assert evaluate_rule(_rule(), _ctx(henrique_gap_s=60.0)).conditions[0].met is True


def test_entering_a_little_past_the_limit_is_not_together() -> None:
    row = evaluate_rule(_rule(), _ctx(henrique_gap_s=61.0)).conditions[0]
    assert row.met is False
    assert row.detail == "Henrique and Kristen arrived at House 1 min 1 sec apart, more than the 1 min limit"


def test_someone_home_for_hours_is_not_together_with_a_new_arrival() -> None:
    # Nine minutes of inside fixes and none outside: no arrival was seen.
    now = _NOW.timestamp()
    home = tuple(_fix(now - 60.0 * minute, inside=True) for minute in range(10))
    ctx = _ctx(henrique_gap_s=10.0, histories={"kristen": _arrival_history(now - 300.0), "henrique": home})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert "Henrique: arrival at House not seen" in row.detail


def test_order_of_arrival_does_not_matter() -> None:
    assert evaluate_rule(_rule(), _ctx(henrique_gap_s=-40.0)).conditions[0].met is True


def test_a_wider_limit_accepts_a_longer_gap() -> None:
    assert evaluate_rule(_rule(max_gap_s=300), _ctx(henrique_gap_s=120.0)).conditions[0].met is True


def test_not_together_when_one_user_is_outside_the_geofence() -> None:
    ctx = _ctx(henrique_gap_s=10.0, user_locations={"henrique": _outside(), "kristen": _inside()})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert row.detail == "Henrique: not inside House"


def test_not_together_when_a_user_has_no_location() -> None:
    ctx = _ctx(henrique_gap_s=10.0, user_locations={"kristen": _inside()})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert row.detail == "Henrique: no location yet"


def test_not_together_when_a_location_is_too_inaccurate_to_trust() -> None:
    ctx = _ctx(henrique_gap_s=10.0, user_locations={"henrique": _inside(accuracy_m=500), "kristen": _inside()})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert row.detail == "Henrique: location ignored (low accuracy)"


def test_not_together_when_a_users_arrival_time_is_unknown() -> None:
    row = evaluate_rule(_rule(), _ctx(henrique_gap_s=None)).conditions[0]
    assert row.met is False
    assert row.detail == (
        "Henrique: arrival at House not seen (no accurate fix outside it before the inside ones in the last 10 min)"
    )


def test_home_wifi_does_not_rescue_a_low_accuracy_fix() -> None:
    # The plain geofence conditions accept a poor fix on the home WiFi; an arrival
    # time must come from a trustworthy fix, so this one stays unmet.
    on_wifi = _inside(accuracy_m=500).model_copy(update={"connection_type": "w", "wifi_ssid": "HomeNet"})
    ctx = _ctx(henrique_gap_s=10.0, user_locations={"henrique": on_wifi, "kristen": _inside()})
    ctx.user_home_wifi_ssid["henrique"] = "HomeNet"
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert row.detail == "Henrique: location ignored (low accuracy)"


def test_unknown_geofence_is_never_together() -> None:
    row = evaluate_rule(_rule(), _ctx(henrique_gap_s=10.0, geofences=())).conditions[0]
    assert row.met is False
    assert row.detail == 'Unknown geofence "house"'


def test_presence_users_are_both_users_only_while_they_arrived_together() -> None:
    assert presence_user_ids_for_rule(_rule(), _ctx(henrique_gap_s=10.0)) == ("henrique", "kristen")
    assert presence_user_ids_for_rule(_rule(), _ctx(henrique_gap_s=600.0)) == ()


def test_condition_needs_at_least_two_users() -> None:
    with pytest.raises(ValidationError):
        UsersArrivedTogetherCondition(type="users_arrived_together", geofence_id="house", user_ids=["henrique"])


@pytest.mark.parametrize("user_ids", [["henrique", "henrique"], ["henrique", " HENRIQUE "]])
def test_condition_rejects_the_same_user_listed_twice(user_ids: list[str]) -> None:
    with pytest.raises(ValidationError, match="Expected 2 distinct user_ids"):
        UsersArrivedTogetherCondition(type="users_arrived_together", geofence_id="house", user_ids=user_ids)


def test_arrived_together_inside_an_any_group_still_lets_an_edge_rule_fire() -> None:
    rule = _rule().model_copy(
        update={"conditions": RuleConditionsOut(all=[AnyConditionsCondition(type="any", conditions=[_condition()])])},
    )
    assert evaluate_rule(rule, _ctx(henrique_gap_s=10.0)).all_met is True
    assert evaluate_rule(rule, _ctx(henrique_gap_s=90.0)).all_met is False


@pytest.mark.parametrize("max_gap_s", [0, 3601])
def test_condition_rejects_a_gap_outside_one_second_to_one_hour(max_gap_s: int) -> None:
    with pytest.raises(ValidationError):
        _condition(max_gap_s=max_gap_s)


def test_condition_round_trips_through_the_rule_model_with_a_default_gap() -> None:
    rule = RuleOut.model_validate(_rule().model_dump(mode="json"))
    condition = rule.conditions.all[0]
    assert isinstance(condition, UsersArrivedTogetherCondition)
    assert condition.max_gap_s == 60


def test_edge_rule_does_not_fire_for_a_user_arriving_alone() -> None:
    # An edge rule treats plain geofence rows as the trigger itself, but this row
    # is a real check on the other user, so it still blocks the fire.
    alone = _ctx(henrique_gap_s=10.0, user_locations={"henrique": _inside(), "kristen": _outside()})
    assert evaluate_rule(_rule(), alone).all_met is False
    assert evaluate_rule(_rule(), _ctx(henrique_gap_s=10.0)).all_met is True
    assert evaluate_rule(_rule(), _ctx(henrique_gap_s=90.0)).all_met is False


def test_a_lone_arrived_together_rule_does_not_turn_on_dwell_arrival_tracking() -> None:
    # It reads each user's stored fixes at its own accuracy limit, not the shared inside-since map.
    rule = _rule()
    assert _geofence_dwell_accuracy_limit_m([rule]) is None
    assert _max_dwell_min_inside_s([_rule(max_gap_s=240)]) == 240.0


def test_each_rule_judges_arrivals_by_its_own_accuracy_limit() -> None:
    now = _NOW.timestamp()
    kristen = (
        _fix(now, inside=True, accuracy_m=5),
        _fix(now - 250.0, inside=True, accuracy_m=5),
        _fix(now - 300.0, inside=True, accuracy_m=30),
        _fix(now - 301.0, inside=False, accuracy_m=5),
    )
    henrique = (
        _fix(now, inside=True, accuracy_m=5),
        _fix(now - 100.0, inside=True, accuracy_m=5),
        _fix(now - 280.0, inside=True, accuracy_m=30),
        _fix(now - 290.0, inside=False, accuracy_m=5),
    )
    ctx = _ctx(
        henrique_gap_s=0.0,
        user_locations={
            "henrique": _fix(now, inside=True, accuracy_m=5),
            "kristen": _fix(now, inside=True, accuracy_m=5),
        },
        histories={"henrique": henrique, "kristen": kristen},
    )
    loose = _rule().model_copy(update={"min_location_accuracy_m": 50})
    strict = _rule().model_copy(update={"min_location_accuracy_m": 10})

    assert evaluate_rule(loose, ctx).conditions[0].detail == (
        "Henrique and Kristen arrived at House 20 sec apart (limit 1 min)"
    )
    assert evaluate_rule(strict, ctx).conditions[0].detail == (
        "Henrique and Kristen arrived at House 2 min 30 sec apart, more than the 1 min limit"
    )


def test_a_poor_fix_outside_does_not_count_as_an_observed_entry() -> None:
    now = _NOW.timestamp()
    henrique = (
        _fix(now, inside=True),
        _fix(now - 200.0, inside=True),
        _fix(now - 201.0, inside=False, accuracy_m=500),
    )
    ctx = _ctx(henrique_gap_s=0.0, histories={"kristen": _arrival_history(now - 200.0), "henrique": henrique})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert "Henrique: arrival at House not seen" in row.detail


def test_the_last_entry_counts_when_a_user_left_and_came_back() -> None:
    now = _NOW.timestamp()
    henrique = (
        _fix(now, inside=True),
        _fix(now - 30.0, inside=True),
        _fix(now - 31.0, inside=False),
        _fix(now - 200.0, inside=True),
        _fix(now - 201.0, inside=False),
    )
    ctx = _ctx(henrique_gap_s=0.0, histories={"kristen": _arrival_history(now - 20.0), "henrique": henrique})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is True
    assert row.detail == "Henrique and Kristen arrived at House 10 sec apart (limit 1 min)"


def test_a_fix_dated_in_the_future_is_not_an_arrival() -> None:
    now = _NOW.timestamp()
    kristen = (_fix(now + 60.0, inside=True), _fix(now, inside=False), _fix(now - 100.0, inside=False))
    ctx = _ctx(
        henrique_gap_s=0.0,
        user_locations={"henrique": _inside(), "kristen": _inside()},
        histories={"kristen": kristen, "henrique": _arrival_history(now - 100.0)},
    )
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert "Kristen: arrival at House not seen" in row.detail


def test_a_fix_without_an_accuracy_still_counts_when_the_rule_allows_it() -> None:
    now = _NOW.timestamp()
    inside = _fix(now - 200.0, inside=True).model_copy(update={"accuracy_m": None})
    outside = _fix(now - 201.0, inside=False).model_copy(update={"accuracy_m": None})
    latest = _fix(now, inside=True).model_copy(update={"accuracy_m": None})
    ctx = _ctx(
        henrique_gap_s=0.0,
        user_locations={"henrique": latest, "kristen": _inside()},
        histories={"kristen": _arrival_history(now - 200.0), "henrique": (latest, inside, outside)},
    )
    assert evaluate_rule(_rule(), ctx).conditions[0].met is True


def test_at_an_exact_tie_the_outside_fix_is_taken_as_the_later_one() -> None:
    now = _NOW.timestamp()
    # The newest instant has both an inside and an outside fix: never invent an arrival from it.
    henrique = (_fix(now, inside=False), _fix(now, inside=True), _fix(now - 100.0, inside=False))
    ctx = _ctx(henrique_gap_s=0.0, histories={"kristen": _arrival_history(now - 100.0), "henrique": henrique})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert "Henrique: arrival at House not seen" in row.detail


def test_a_fix_whose_accuracy_circle_straddles_the_edge_is_not_an_exit() -> None:
    now = _NOW.timestamp()
    # About 252 m from the center with a 50 m accuracy circle: outside by geometry, but not clearly.
    edge = UserLocationOut(
        accuracy_m=50,
        lat=41.194072 + 252.0 / 111_194.9,
        lon=-73.888325,
        fix_at=_fix(now - 100.0, inside=True).fix_at,
        reported_at=_fix(now - 100.0, inside=True).reported_at,
        source="owntracks",
    )
    henrique = (_fix(now, inside=True), _fix(now - 50.0, inside=True), edge, _fix(now - 150.0, inside=True))
    ctx = _ctx(henrique_gap_s=0.0, histories={"kristen": _arrival_history(now - 20.0), "henrique": henrique})
    row = evaluate_rule(_rule(), ctx).conditions[0]
    assert row.met is False
    assert "Henrique: arrival at House not seen" in row.detail


def test_presence_users_are_empty_while_an_arrival_is_unseen() -> None:
    assert presence_user_ids_for_rule(_rule(), _ctx(henrique_gap_s=None)) == ()
    assert presence_user_ids_for_rule(_rule(), _ctx(henrique_gap_s=10.0)) == ("henrique", "kristen")
