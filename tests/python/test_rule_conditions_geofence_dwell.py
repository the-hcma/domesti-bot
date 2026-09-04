"""Unit tests for users_inside/outside_geofence_for_s condition evaluation."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterLocalTimeCondition,
    GeofenceOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import RuleTrigger
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, evaluate_rule
from app.rule_validation import build_roster_user_id_lookup

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")
_GEOFENCE = GeofenceOut(
    geofence_id="house",
    label="House",
    center_lat=41.194072,
    center_lon=-73.888325,
    radius_m=250,
    enabled=True,
)


def test_inside_geofence_for_s_pending_when_time_gate_just_opened() -> None:
    # Henrique has been home for hours, well before the 21:00 gate opened —
    # but the gate itself only opened 1s ago, so "home for 10 min after 9pm"
    # is not yet satisfied. The raw pre-gate streak must not count.
    now = datetime(2026, 9, 2, 21, 0, 1, tzinfo=_TZ)
    inside_since = now.timestamp() - 7200.0
    result = evaluate_rule(
        _inside_for_s_rule(min_inside_s=600, time_hhmm="21:00"),
        _ctx(now=now, geofence_inside_since={("henrique", "house"): inside_since}),
    )
    assert result.all_met is False
    dwell_condition = next(row for row in result.conditions if "Inside" in row.label)
    assert dwell_condition.met is False
    assert "need" in dwell_condition.detail


def test_inside_geofence_for_s_met_once_gate_open_for_min_duration() -> None:
    now = datetime(2026, 9, 2, 21, 10, 0, tzinfo=_TZ)
    inside_since = now.timestamp() - 7200.0
    result = evaluate_rule(
        _inside_for_s_rule(min_inside_s=600, time_hhmm="21:00"),
        _ctx(now=now, geofence_inside_since={("henrique", "house"): inside_since}),
    )
    assert result.all_met is True


def test_inside_geofence_for_s_unclamped_when_streak_starts_after_gate() -> None:
    now = datetime(2026, 9, 2, 21, 30, 0, tzinfo=_TZ)
    inside_since = now.timestamp() - 700.0
    result = evaluate_rule(
        _inside_for_s_rule(min_inside_s=600, time_hhmm="21:00"),
        _ctx(now=now, geofence_inside_since={("henrique", "house"): inside_since}),
    )
    assert result.all_met is True


def test_outside_geofence_for_s_pending_when_time_gate_just_opened() -> None:
    now = datetime(2026, 9, 2, 21, 0, 1, tzinfo=_TZ)
    outside_since = now.timestamp() - 7200.0
    result = evaluate_rule(
        _outside_for_s_rule(min_outside_s=600, time_hhmm="21:00"),
        _ctx(
            now=now,
            geofence_outside_since={("henrique", "house"): outside_since},
            user_far_from_geofence=True,
        ),
    )
    assert result.all_met is False


def test_outside_geofence_for_s_met_once_gate_open_for_min_duration() -> None:
    now = datetime(2026, 9, 2, 21, 10, 0, tzinfo=_TZ)
    outside_since = now.timestamp() - 7200.0
    result = evaluate_rule(
        _outside_for_s_rule(min_outside_s=600, time_hhmm="21:00"),
        _ctx(
            now=now,
            geofence_outside_since={("henrique", "house"): outside_since},
            user_far_from_geofence=True,
        ),
    )
    assert result.all_met is True


def _ctx(
    *,
    now: datetime,
    geofence_inside_since: dict[tuple[str, str], float] | None = None,
    geofence_outside_since: dict[tuple[str, str], float] | None = None,
    user_far_from_geofence: bool = False,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique"}
    lat, lon = (44.0, -73.0) if user_far_from_geofence else (41.194085, -73.888365)
    reported_iso = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return RuleEvaluationContext(
        geofences=(_GEOFENCE,),
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(list(user_display_names.keys())),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations={
            "henrique": UserLocationOut(
                lat=lat,
                lon=lon,
                accuracy_m=20,
                fix_at=reported_iso,
                reported_at=reported_iso,
                source="test",
            ),
        },
        geofence_inside_since=geofence_inside_since or {},
        geofence_outside_since=geofence_outside_since or {},
    )


def _inside_for_s_rule(*, min_inside_s: int, time_hhmm: str) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterLocalTimeCondition(type="after_local_time", time_hhmm=time_hhmm),
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=min_inside_s,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="home-dwell-after-local-time",
        label="Home dwell after 9pm",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _outside_for_s_rule(*, min_outside_s: int, time_hhmm: str) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterLocalTimeCondition(type="after_local_time", time_hhmm=time_hhmm),
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=min_outside_s,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="away-dwell-after-local-time",
        label="Away dwell after 9pm",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )
