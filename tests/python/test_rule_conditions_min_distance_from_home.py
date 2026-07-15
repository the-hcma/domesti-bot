"""Tests for users_min_distance_from_home_m rule conditions."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.api.schemas import (
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersMinDistanceFromHomeMCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_validation import build_roster_user_id_lookup, collect_rule_user_ids


def test_collect_rule_user_ids_includes_min_distance_condition() -> None:
    assert collect_rule_user_ids(_rule()) == {"henrique", "kristen"}


def test_users_min_distance_from_home_m_met_when_all_far_enough() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=_SETTINGS,
    ):
        result = evaluate_rule(
            _rule(),
            _ctx(
                now=now,
                user_locations={
                    "henrique": _far_location(),
                    "kristen": _far_location(),
                },
            ),
        )
    assert result.conditions[0].met is True
    assert "at least 80 km from Home" in result.conditions[0].detail
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=_SETTINGS,
    ):
        assert presence_user_ids_for_rule(
            _rule(),
            _ctx(
                now=now,
                user_locations={
                    "henrique": _far_location(),
                    "kristen": _far_location(),
                },
            ),
        ) == ("henrique", "kristen")


def test_users_min_distance_from_home_m_unmet_for_low_accuracy() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    fuzzy = UserLocationOut(
        accuracy_m=500,
        lat=_FAR_LAT,
        lon=_FAR_LON,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=_SETTINGS,
    ):
        result = evaluate_rule(
            _rule(user_ids=["henrique"]),
            _ctx(now=now, user_locations={"henrique": fuzzy}),
        )
    assert result.conditions[0].met is False
    assert "location ignored" in result.conditions[0].detail


def test_users_min_distance_from_home_m_unmet_for_unconfigured_home() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    unset = SettingsLocationOut(
        home_label="Unset",
        lat=0.0,
        lon=0.0,
        timezone="UTC",
    )
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=unset,
    ):
        result = evaluate_rule(
            _rule(user_ids=["henrique"]),
            _ctx(
                now=now,
                user_locations={"henrique": _far_location()},
            ),
        )
    assert result.conditions[0].met is False
    assert "Home location is not configured" in result.conditions[0].detail


def test_users_min_distance_from_home_m_unmet_when_any_near_home() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=_SETTINGS,
    ):
        result = evaluate_rule(
            _rule(),
            _ctx(
                now=now,
                user_locations={
                    "henrique": _far_location(),
                    "kristen": _near_location(),
                },
            ),
        )
    assert result.conditions[0].met is False
    assert "Kristen" in result.conditions[0].detail
    assert "need ≥ 80 km" in result.conditions[0].detail


def test_users_min_distance_from_home_m_unmet_without_location() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    with patch(
        "app.rule_conditions.load_settings_location",
        return_value=_SETTINGS,
    ):
        result = evaluate_rule(
            _rule(user_ids=["henrique"]),
            _ctx(now=now, user_locations={}),
        )
    assert result.conditions[0].met is False
    assert "no location yet" in result.conditions[0].detail


_FAR_LAT = 42.0
_FAR_LON = -73.8883254
_MIN_DISTANCE_M = 80_000.0
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def _ctx(
    *,
    now: datetime,
    user_locations: dict[str, UserLocationOut] | None = None,
    user_display_names: dict[str, str] | None = None,
) -> RuleEvaluationContext:
    names = user_display_names or {
        "henrique": "Henrique",
        "kristen": "Kristen",
    }
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    return RuleEvaluationContext(
        geofences=(),
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(list(names.keys())),
        sun=sun,
        timezone=_TZ,
        user_display_names=names,
        user_locations=user_locations or {},
    )


def _far_location() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=20,
        lat=_FAR_LAT,
        lon=_FAR_LON,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def _near_location() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def _rule(*, user_ids: list[str] | None = None) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersMinDistanceFromHomeMCondition(
                    type="users_min_distance_from_home_m",
                    min_distance_m=_MIN_DISTANCE_M,
                    user_ids=user_ids or ["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="far-from-home",
        label="Far from home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
