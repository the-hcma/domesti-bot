"""users_together: confidence that listed users travel together, evaluated from stored histories."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    AnyConditionsCondition,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersTogetherCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_evaluator import _format_rule_conditions_for_log
from app.rule_validation import build_roster_user_id_lookup, collect_rule_user_ids

_NOW = 1_800_000_000.0
_TZ = ZoneInfo("America/New_York")
_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_LAT0 = 41.0
_LON0 = -73.9
_SETTINGS = SettingsLocationOut(home_label="Home", lat=_LAT0, lon=_LON0, timezone="America/New_York")


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def _location(
    at: float,
    *,
    accuracy_m: int | None = 5,
    connection_type: str | None = None,
    lateral_m: float = 0.0,
    speed_mps: float = 20.0,
    wifi_ssid: str | None = None,
) -> UserLocationOut:
    """A fix on a straight road heading north, ``lateral_m`` meters east of it."""
    lat = _LAT0 + speed_mps * (at - _NOW) / _M_PER_DEG
    lon = _LON0 + lateral_m / (_M_PER_DEG * math.cos(math.radians(_LAT0)))
    return UserLocationOut(
        accuracy_m=accuracy_m,
        connection_type=connection_type,
        fix_at=_iso(at),
        lat=lat,
        lon=lon,
        reported_at=_iso(at),
        source="test",
        wifi_ssid=wifi_ssid,
    )


def _history(
    *,
    every_s: float = 10.0,
    last_at: float = _NOW,
    span_s: float = 700.0,
    **kwargs: Any,
) -> list[UserLocationOut]:
    count = int(span_s // every_s)
    return [_location(last_at - index * every_s, **kwargs) for index in range(count + 1)]


def _ctx(
    histories: dict[str, list[UserLocationOut]], *, home_ssid: dict[str, str | None] | None = None
) -> RuleEvaluationContext:
    """A context whose per-user history is newest-first and whose latest location is the newest fix."""
    names = {user_id: user_id.upper() for user_id in histories}
    return RuleEvaluationContext(
        geofences=(),
        now=datetime.fromtimestamp(_NOW, tz=_TZ),
        roster_user_id_lookup=build_roster_user_id_lookup(list(names)),
        sun=compute_rules_sun_out(_SETTINGS, now=datetime.fromtimestamp(_NOW, tz=_TZ)),
        timezone=_TZ,
        user_display_names=names,
        user_home_wifi_ssid=home_ssid or {},
        user_location_history={
            user_id: tuple(sorted(rows, key=lambda row: row.fix_at, reverse=True))
            for user_id, rows in histories.items()
        },
        user_locations={user_id: max(rows, key=lambda row: row.fix_at) for user_id, rows in histories.items() if rows},
    )


def _rule(
    condition: RuleConditionOut | None = None,
    *,
    min_location_accuracy_m: int = 50,
    **condition_kwargs: Any,
) -> RuleOut:
    chosen = condition or UsersTogetherCondition(
        type="users_together",
        user_ids=condition_kwargs.pop("user_ids", ["user-a", "user-b"]),
        **condition_kwargs,
    )
    return RuleOut(
        conditions=RuleConditionsOut(all=[chosen]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="together",
        label="Together",
        min_location_accuracy_m=min_location_accuracy_m,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def _row(rule: RuleOut, ctx: RuleEvaluationContext) -> Any:
    return evaluate_rule(rule, ctx).conditions[0]


def _pair(
    *, lateral_m: float = 3.0, b_every_s: float = 19.0, b_last_at: float = _NOW - 3.0
) -> dict[str, list[UserLocationOut]]:
    return {
        "user-a": _history(),
        "user-b": _history(every_s=b_every_s, last_at=b_last_at, lateral_m=lateral_m),
    }


def test_two_users_travelling_together_meet_the_condition_and_the_status_shows_the_values() -> None:
    row = _row(_rule(), _ctx(_pair()))

    assert row.met is True
    assert row.label == "Travelling together (USER-A and USER-B)"
    assert "confidence 1.00 (1 min 1.00, 5 min 1.00, 10 min 1.00)" in row.detail
    assert "apart at the latest shared fix" in row.detail
    assert "newest fixes" in row.detail
    assert "needs 0.80" in row.detail


def test_users_on_different_roads_do_not_meet_it_and_do_meet_the_apart_form() -> None:
    ctx = _ctx(_pair(lateral_m=400.0))

    assert _row(_rule(), ctx).met is False
    apart = _row(_rule(expected=False), ctx)
    assert apart.met is True
    assert apart.label == "Not travelling together (USER-A and USER-B)"
    assert "apart needs at most 0.20" in apart.detail


def test_a_steady_hundred_meter_gap_is_in_the_dead_zone_and_meets_neither_form() -> None:
    ctx = _ctx(_pair(b_every_s=10.0, b_last_at=_NOW, lateral_m=100.0))

    assert _row(_rule(), ctx).met is False
    assert _row(_rule(expected=False), ctx).met is False


def test_a_lower_minimum_confidence_widens_what_counts_as_together() -> None:
    # 80 m apart with 5 m accuracy each: closeness (160 - 80) / (160 - 40) = 0.67.
    ctx = _ctx(_pair(b_every_s=10.0, b_last_at=_NOW, lateral_m=80.0))

    assert _row(_rule(), ctx).met is False
    assert _row(_rule(min_confidence=0.6), ctx).met is True


def test_the_latest_fix_is_not_counted_twice_when_it_is_also_in_the_history() -> None:
    detail = _row(_rule(), _ctx(_pair())).detail

    assert "low-quality" not in detail


def test_a_user_without_any_location_makes_the_condition_unmet_in_both_forms() -> None:
    ctx = _ctx({"user-a": _history(), "user-b": []})

    for expected in (True, False):
        row = _row(_rule(expected=expected), ctx)
        assert row.met is False
        assert "USER-B has no fix with a reported accuracy of 50 m or better" in row.detail


def test_a_stale_second_phone_is_reported_with_its_age() -> None:
    ctx = _ctx(_pair(b_last_at=_NOW - 120.0))

    row = _row(_rule(), ctx)

    assert row.met is False
    assert "unknown" in row.detail
    assert "USER-B's newest usable fix is 2 min old" in row.detail
    assert "at most 45 sec allowed" in row.detail


def test_too_little_data_in_the_last_minute_is_unknown_not_apart() -> None:
    ctx = _ctx(
        {"user-a": _history(), "user-b": [_location(_NOW - 3.0, lateral_m=3.0), _location(_NOW - 100.0, lateral_m=3.0)]}
    )

    for expected in (True, False):
        row = _row(_rule(expected=expected), ctx)
        assert row.met is False
        assert "too few usable fixes in the last minute to compare them (" in row.detail


def test_the_rules_accuracy_limit_decides_which_fixes_are_usable() -> None:
    histories = {
        "user-a": _history(accuracy_m=20),
        "user-b": _history(every_s=19.0, last_at=_NOW - 3.0, accuracy_m=20, lateral_m=3.0),
    }

    assert _row(_rule(min_location_accuracy_m=50), _ctx(histories)).met is True
    strict = _row(_rule(min_location_accuracy_m=10), _ctx(histories))
    assert strict.met is False
    assert "reported accuracy of 10 m or better" in strict.detail


def test_the_home_wifi_exception_does_not_rescue_a_poor_fix_but_a_good_fix_on_it_counts() -> None:
    def user_b(accuracy_m: int) -> dict[str, list[UserLocationOut]]:
        return {
            "user-a": _history(),
            "user-b": _history(
                every_s=19.0,
                last_at=_NOW - 3.0,
                accuracy_m=accuracy_m,
                connection_type="w",
                wifi_ssid="HomeNet",
                lateral_m=3.0,
            ),
        }

    home: dict[str, str | None] = {"user-b": "HomeNet"}

    poor = _row(_rule(), _ctx(user_b(80), home_ssid=home))
    good = _row(_rule(), _ctx(user_b(8), home_ssid=home))

    assert poor.met is False
    assert "has no fix with a reported accuracy of 50 m or better" in poor.detail
    assert good.met is True


def test_a_missing_accuracy_counts_as_low_quality() -> None:
    ctx = _ctx(
        {"user-a": _history(), "user-b": _history(every_s=19.0, last_at=_NOW - 3.0, accuracy_m=None, lateral_m=3.0)}
    )

    assert _row(_rule(), ctx).met is False


def test_every_pair_of_three_users_must_satisfy_the_condition() -> None:
    together = _pair()
    third_close = {**together, "user-c": _history(every_s=15.0, last_at=_NOW - 2.0, lateral_m=5.0)}
    third_far = {**together, "user-c": _history(every_s=15.0, last_at=_NOW - 2.0, lateral_m=500.0)}
    rule = _rule(user_ids=["user-a", "user-b", "user-c"])

    assert _row(rule, _ctx(third_close)).met is True
    assert _row(rule, _ctx(third_far)).met is False
    assert _row(_rule(user_ids=["user-a", "user-b", "user-c"], expected=False), _ctx(third_far)).met is False


def test_one_unknown_pair_among_three_users_keeps_the_condition_unmet() -> None:
    histories = {**_pair(), "user-c": []}

    for expected in (True, False):
        assert _row(_rule(user_ids=["user-a", "user-b", "user-c"], expected=expected), _ctx(histories)).met is False


def test_a_reference_that_is_not_in_the_roster_is_unmet() -> None:
    row = _row(_rule(user_ids=["user-a", "someone-else"]), _ctx(_pair()))

    assert row.met is False
    assert '"someone-else": not in user roster' in row.detail


def test_users_who_are_together_are_the_presence_users_and_apart_users_are_not() -> None:
    assert presence_user_ids_for_rule(_rule(), _ctx(_pair())) == ("user-a", "user-b")
    assert presence_user_ids_for_rule(_rule(), _ctx(_pair(lateral_m=400.0))) == ()


def test_the_condition_works_inside_an_any_group() -> None:
    together = UsersTogetherCondition(type="users_together", user_ids=["user-a", "user-b"])
    rule = _rule(AnyConditionsCondition(type="any", conditions=[together]))

    assert evaluate_rule(rule, _ctx(_pair())).all_met is True
    assert evaluate_rule(rule, _ctx(_pair(lateral_m=400.0))).all_met is False


def test_the_users_of_the_condition_are_collected_for_the_rule() -> None:
    assert collect_rule_user_ids(_rule()) == {"user-a", "user-b"}


def test_the_schema_defaults_and_round_trip() -> None:
    rule = RuleOut.model_validate(_rule().model_dump(mode="json"))
    condition = rule.conditions.all[0]

    assert isinstance(condition, UsersTogetherCondition)
    assert (condition.expected, condition.min_confidence, condition.near_m, condition.far_m) == (True, 0.8, 30.0, 150.0)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"user_ids": ["user-a"]},
        {"user_ids": ["user-a", " USER-A "]},
        {"user_ids": ["user-a", "user-b"], "min_confidence": 0.5},
        {"user_ids": ["user-a", "user-b"], "min_confidence": math.nan},
        {"user_ids": ["user-a", "user-b"], "far_m": math.inf},
        {"user_ids": ["user-a", "user-b"], "far_m": math.nan},
        {"user_ids": ["user-a", "user-b"], "far_m": 150_000.0},
        {"user_ids": ["user-a", "user-b"], "near_m": math.inf},
        {"user_ids": ["user-a", "user-b"], "min_confidence": 0.4},
        {"user_ids": ["user-a", "user-b"], "min_confidence": 1.1},
        {"user_ids": ["user-a", "user-b"], "near_m": 100.0, "far_m": 100.0},
        {"user_ids": ["user-a", "user-b"], "near_m": -1.0},
    ],
)
def test_the_schema_rejects_bad_shapes(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        UsersTogetherCondition(type="users_together", **kwargs)


def _dead_zone_ctx() -> RuleEvaluationContext:
    return _ctx(_pair(b_every_s=10.0, b_last_at=_NOW, lateral_m=100.0))


def test_the_status_says_when_the_users_are_in_between() -> None:
    assert "in between, so neither together nor apart" in _row(_rule(), _dead_zone_ctx()).detail
    assert "in between" not in _row(_rule(), _ctx(_pair())).detail


def test_the_status_mentions_how_long_ago_the_shared_fix_was_when_a_phone_lags() -> None:
    detail = _row(_rule(), _ctx(_pair(b_last_at=_NOW - 40.0))).detail

    assert "apart at the latest shared fix (40 sec ago)" in detail


def test_with_the_history_the_evaluator_really_loads_the_longest_window_is_thinner_but_known() -> None:
    # The evaluator reads 600 s of history; with the other phone 40 s behind, the oldest instants lack an earlier fix.
    histories = {
        "user-a": _history(span_s=600.0),
        "user-b": _history(every_s=10.0, last_at=_NOW - 40.0, span_s=560.0, lateral_m=3.0),
    }

    row = _row(_rule(), _ctx(histories))

    assert row.met is True
    assert "10 min 1.00" in row.detail


def test_a_fix_time_that_cannot_be_parsed_falls_back_to_the_report_time_and_an_unusable_row_is_skipped() -> None:
    def broken_fix_time(location: UserLocationOut) -> UserLocationOut:
        return location.model_copy(update={"fix_at": "not-a-time"})

    def broken_both(location: UserLocationOut) -> UserLocationOut:
        return location.model_copy(update={"fix_at": "not-a-time", "reported_at": "also-not-a-time"})

    base = _pair()
    fallback = {"user-a": base["user-a"], "user-b": [broken_fix_time(row) for row in base["user-b"]]}
    unusable = {"user-a": base["user-a"], "user-b": [broken_both(row) for row in base["user-b"]]}

    assert _row(_rule(), _ctx(fallback)).met is True
    skipped = _row(_rule(), _ctx(unusable))
    assert skipped.met is False
    assert "USER-B has no fix with a reported accuracy" in skipped.detail


def test_the_apart_form_and_three_users_give_the_expected_presence_users() -> None:
    assert presence_user_ids_for_rule(_rule(expected=False), _ctx(_pair(lateral_m=400.0))) == ("user-a", "user-b")
    assert presence_user_ids_for_rule(_rule(expected=False), _ctx(_pair())) == ()
    third = {**_pair(), "user-c": _history(every_s=15.0, last_at=_NOW - 2.0, lateral_m=5.0)}
    assert presence_user_ids_for_rule(_rule(user_ids=["user-a", "user-b", "user-c"]), _ctx(third)) == (
        "user-a",
        "user-b",
        "user-c",
    )


def test_an_unknown_pair_next_to_a_clearly_failing_pair_is_still_unmet() -> None:
    histories = {**_pair(lateral_m=400.0), "user-c": []}

    for expected in (True, False):
        rule = _rule(user_ids=["user-a", "user-b", "user-c"], expected=expected)
        assert _row(rule, _ctx(histories)).met is False


def test_inside_an_any_group_an_unknown_condition_does_not_satisfy_the_group_but_a_met_sibling_does() -> None:
    unknown_together = UsersTogetherCondition(type="users_together", user_ids=["user-a", "user-b"])
    sibling = UsersTogetherCondition(type="users_together", user_ids=["user-a", "user-c"])
    histories = {**_pair(), "user-c": _history(every_s=15.0, last_at=_NOW - 2.0, lateral_m=5.0)}
    stale_b = {**histories, "user-b": _history(every_s=19.0, last_at=_NOW - 120.0, lateral_m=3.0)}
    rule = _rule(AnyConditionsCondition(type="any", conditions=[unknown_together, sibling]))

    assert evaluate_rule(rule, _ctx(stale_b)).all_met is True
    assert evaluate_rule(rule, _ctx({**stale_b, "user-c": []})).all_met is False


def test_the_latest_fix_is_counted_once_even_when_the_history_already_holds_it() -> None:
    histories = _pair()
    estimate_detail = _row(_rule(), _ctx(histories)).detail

    assert "low-quality" not in estimate_detail
    only_latest = {"user-a": histories["user-a"][:1], "user-b": histories["user-b"][:1]}
    assert (
        "too few usable fixes in the last minute to compare them (0 of 7 instants)"
        in _row(_rule(), _ctx(only_latest)).detail
    )


def test_the_fired_log_line_carries_the_confidence_detail() -> None:
    rule = _rule()
    evaluation = evaluate_rule(rule, _ctx(_pair()))

    logged = _format_rule_conditions_for_log(rule, evaluation, fire_source="immediate")

    assert logged.startswith("Travelling together (USER-A and USER-B)=met (USER-A and USER-B: confidence 1.00")
