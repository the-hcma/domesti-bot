"""Unit tests for ``rule_eligible_since`` — the clamp instant for pre-eligibility signal."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AfterLocalTimeCondition,
    AfterSunsetCondition,
    AllConditionsCondition,
    AnyConditionsCondition,
    BeforeLocalTimeCondition,
    BeforeSunriseCondition,
    DevicesAnyInStateForSCondition,
    GeofenceOut,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, rule_eligible_since
from app.rule_validation import build_roster_user_id_lookup

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def test_rule_eligible_since_none_when_no_temporal_gate() -> None:
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions([])
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_none_when_after_local_time_not_yet_open() -> None:
    now = datetime(2026, 9, 2, 20, 59, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00")],
    )
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_after_local_time_opens_today() -> None:
    now = datetime(2026, 9, 2, 21, 10, 13, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00")],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_same_day_start() -> None:
    now = datetime(2026, 9, 2, 9, 30, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="10:00",
                start_hhmm="09:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 9, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_overnight_wrap_tail_opened_yesterday() -> None:
    # Window is 22:00-06:00 (wraps past midnight); "now" is in the early-morning
    # tail, so the window that is currently open actually opened yesterday.
    now = datetime(2026, 9, 3, 1, 30, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="06:00",
                start_hhmm="22:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 22, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_local_time_window_overnight_wrap_evening_portion_opened_today() -> None:
    now = datetime(2026, 9, 2, 23, 15, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="06:00",
                start_hhmm="22:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 22, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_after_sunset_opens_at_sunset_plus_offset() -> None:
    now = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ)
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    sunset_at = datetime.fromisoformat(sun.sunset_at.replace("Z", "+00:00")).astimezone(_TZ)
    rule = _rule_with_conditions([AfterSunsetCondition(type="after_sunset", offset_minutes=15)])
    expected = (sunset_at.replace(second=0, microsecond=0) + timedelta(minutes=15)).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_before_local_time_reopens_at_midnight() -> None:
    # before_* gates reopen at local midnight each day (unmet from the close
    # time until then) — a streak that predates midnight must not count as
    # satisfying a dwell condition the instant the gate reopens.
    now = datetime(2026, 9, 2, 8, 0, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [BeforeLocalTimeCondition(type="before_local_time", time_hhmm="10:00")],
    )
    expected = datetime(2026, 9, 2, 0, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_before_local_time_none_after_gate_closes() -> None:
    now = datetime(2026, 9, 2, 10, 30, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [BeforeLocalTimeCondition(type="before_local_time", time_hhmm="10:00")],
    )
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_before_sunrise_reopens_at_midnight() -> None:
    now = datetime(2026, 9, 2, 5, 0, tzinfo=_TZ)
    rule = _rule_with_conditions([BeforeSunriseCondition(type="before_sunrise", offset_minutes=0)])
    expected = datetime(2026, 9, 2, 0, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_descends_into_nested_all_group() -> None:
    # Every child of a nested "all" group must hold too, so a temporal gate
    # buried inside one constrains eligibility exactly like a top-level gate.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AllConditionsCondition(
                type="all",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                ],
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_any_group_clamps_when_no_alternative_satisfies_it() -> None:
    # Both children of this "any" are temporal gates, but only after_local_time
    # is actually open right now (before_local_time("06:00") closed hours
    # ago) — there is no non-temporal alternative either, so the group's
    # truth genuinely depends on the one open gate, which clamps.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                    BeforeLocalTimeCondition(type="before_local_time", time_hhmm="06:00"),
                ],
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_any_group_uses_earliest_of_multiple_open_alternatives() -> None:
    # Both children are temporal gates AND both are currently open:
    # after_local_time("21:00") opened at 21:00, but before_local_time("23:00")
    # has been open continuously since local midnight. An "any" group needs
    # only one alternative, so it has been satisfiable since the *earliest*
    # open child (midnight) — not the latest (21:00). Using max here would
    # wrongly discard dwell accrued all day via the before-gate.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                    BeforeLocalTimeCondition(type="before_local_time", time_hhmm="23:00"),
                ],
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 0, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


def test_rule_eligible_since_any_group_no_clamp_when_alternative_already_met() -> None:
    # The geofence branch is currently true, so the any-group is satisfied
    # without needing after_local_time at all — no clamp.
    now = datetime(2026, 9, 2, 18, 0, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                    UsersInsideGeofenceCondition(
                        type="users_inside_geofence",
                        geofence_id="house",
                        user_ids=["henrique"],
                    ),
                ],
            ),
        ],
    )
    ctx = _ctx(now=now, geofences=(_HOUSE_GEOFENCE,), user_inside_house=True)
    assert rule_eligible_since(rule, ctx) is None


def test_rule_eligible_since_any_group_clamps_when_alternative_not_met() -> None:
    # Same shape, but the geofence branch is currently false — the any-group
    # can only be true via after_local_time right now, so it clamps.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                    UsersInsideGeofenceCondition(
                        type="users_inside_geofence",
                        geofence_id="house",
                        user_ids=["henrique"],
                    ),
                ],
            ),
        ],
    )
    ctx = _ctx(now=now, geofences=(_HOUSE_GEOFENCE,), user_inside_house=False)
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, ctx) == expected


def test_rule_eligible_since_any_group_with_dwell_sibling_does_not_recurse() -> None:
    # Recursion-hazard shape: a *_for_s duration condition sitting directly
    # alongside a temporal gate in the same any-group. _evaluate_devices_any_in_state_for_s
    # calls rule_eligible_since, so naively re-evaluating this sibling to
    # check "is the group already satisfied" would recurse back into itself.
    # It must be excluded from the safe-to-evaluate allowlist and treated
    # conservatively (still clamps) instead.
    #
    # device_state must be populated: _evaluate_devices_any_in_state_for_s
    # returns met=False *before* its own rule_eligible_since call when
    # device_state is None (discovery not ready), so with the default empty
    # ctx this test would still pass even if the exclusion regressed —
    # it needs to reach that call to actually exercise the recursion it
    # claims to rule out.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                    DevicesAnyInStateForSCondition(
                        type="devices_any_in_state_for_s",
                        devices=[
                            RuleConditionDeviceRefOut(
                                device_id="28:05:a5:28:c8:48",
                                family_id=DeviceFamilyId.EP1,
                            ),
                        ],
                        min_duration_s=10,
                        state=DeviceConditionState.CLEAR,
                    ),
                ],
            ),
        ],
    )
    device_state = _ep1_device_state(_FakeEp1Sensor("28:05:a5:28:c8:48", "Office EP1", occupied=False))
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now, device_state=device_state)) == expected


def test_rule_eligible_since_any_group_ignores_gate_nested_deeper_than_direct_child() -> None:
    # A temporal gate nested inside an "all" that is itself inside an "any"
    # is deeper than the direct-child case this fix handles (tracked as a
    # further follow-up) — it is not clamped, but must not crash either.
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AnyConditionsCondition(
                type="any",
                conditions=[
                    AllConditionsCondition(
                        type="all",
                        conditions=[
                            AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                        ],
                    ),
                ],
            ),
        ],
    )
    assert rule_eligible_since(rule, _ctx(now=now)) is None


def test_rule_eligible_since_returns_latest_of_multiple_gates() -> None:
    now = datetime(2026, 9, 2, 21, 10, tzinfo=_TZ)
    rule = _rule_with_conditions(
        [
            AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
            LocalTimeWindowCondition(
                type="local_time_window",
                end_hhmm="23:00",
                start_hhmm="20:00",
            ),
        ],
    )
    expected = datetime(2026, 9, 2, 21, 0, tzinfo=_TZ).timestamp()
    assert rule_eligible_since(rule, _ctx(now=now)) == expected


_HOUSE_GEOFENCE = GeofenceOut(
    geofence_id="house",
    label="House",
    center_lat=41.194072,
    center_lon=-73.888325,
    radius_m=250,
    enabled=True,
)


def _ctx(
    *,
    now: datetime,
    geofences: tuple[GeofenceOut, ...] = (),
    user_inside_house: bool | None = None,
    device_state: DeviceManagersState | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique"}
    user_locations: dict[str, UserLocationOut] = {}
    if user_inside_house is not None:
        lat, lon = (41.194085, -73.888365) if user_inside_house else (44.0, -73.0)
        reported_iso = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        user_locations["henrique"] = UserLocationOut(
            lat=lat,
            lon=lon,
            accuracy_m=20,
            fix_at=reported_iso,
            reported_at=reported_iso,
            source="test",
        )
    return RuleEvaluationContext(
        geofences=geofences,
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(list(user_display_names.keys())),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations=user_locations,
        device_state=device_state,
    )


class _FakeEp1Sensor:
    def __init__(self, identifier: str, label: str, *, occupied: bool) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self._occupancy_bool = occupied

    @property
    def occupancy_state(self) -> str:
        return DeviceConditionState.OCCUPIED.value if self._occupancy_bool else DeviceConditionState.CLEAR.value


def _ep1_device_state(*sensors: _FakeEp1Sensor) -> DeviceManagersState:
    mgr = MagicMock(spec=Ep1DeviceManager)
    mgr.devices = tuple(sensors)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=mgr,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _dwell_condition() -> DevicesAnyInStateForSCondition:
    return DevicesAnyInStateForSCondition(
        type="devices_any_in_state_for_s",
        devices=[
            RuleConditionDeviceRefOut(
                device_id="28:05:a5:28:c8:48",
                family_id=DeviceFamilyId.EP1,
            ),
        ],
        min_duration_s=10,
        state=DeviceConditionState.CLEAR,
    )


def _rule_with_conditions(conditions: list[RuleConditionOut]) -> RuleOut:
    # dwell_satisfied rules require a *_for_s / devices_any_in_state_for_s
    # condition; append one so temporal-gate-only condition lists stay valid.
    # rule_eligible_since ignores it — it only inspects the temporal gates.
    return RuleOut(
        conditions=RuleConditionsOut(all=[*conditions, _dwell_condition()]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="eligibility-test-rule",
        label="Eligibility test rule",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )
