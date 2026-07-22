"""Unit tests for server-side automation rule condition evaluation."""

from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    AnyConditionsCondition,
    DaylightCondition,
    DevicesAllInStateCondition,
    DevicesAnyInStateCondition,
    GeofenceOut,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    UsersOutsideGeofenceForSCondition,
)
from app.automation_rules_loader import load_automation_rules_bundle
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleEvaluationCause, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_actions import cached_kasa_is_on
from app.rule_conditions import (
    RuleEvaluationContext,
    _effective_location_for_rule,
    _presence_user_ids_for_condition,
    compute_rules_sun_out,
    dwell_episode_blocks_fire,
    evaluate_rule,
    presence_user_ids_for_rule,
)
from app.rule_validation import build_roster_user_id_lookup
from app.rules_status import build_rules_status
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def _evening_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
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


def _evening_arrival_any_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
                AnyConditionsCondition(
                    type="any",
                    conditions=[
                        UsersInsideGeofenceCondition(
                            type="users_inside_geofence",
                            geofence_id="house",
                            user_ids=["henrique"],
                        ),
                        UsersInsideGeofenceCondition(
                            type="users_inside_geofence",
                            geofence_id="house",
                            user_ids=["kristen"],
                        ),
                    ],
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


def _evening_window_open_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
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
        fire_once_per_local_day=True,
        id="evening-window-open",
        label="Evening window open",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE, RuleTrigger.SCHEDULED],
    )


def _ctx(
    *,
    now: datetime,
    device_state: DeviceManagersState | None = None,
    geofences: tuple[GeofenceOut, ...] = (),
    geofence_inside_since: dict[tuple[str, str], float] | None = None,
    geofence_outside_since: dict[tuple[str, str], float] | None = None,
    geofence_presence_episode: dict[tuple[str, str], int] | None = None,
    scheduled_outside_dwell_consumed_episode: dict[tuple[str, str, str], int] | None = None,
    user_location_history: dict[str, tuple[UserLocationOut, ...]] | None = None,
    user_locations: dict[str, UserLocationOut] | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique", "kristen": "Kristen"}
    return RuleEvaluationContext(
        geofences=geofences,
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(
            list(user_display_names.keys()),
        ),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations=user_locations or {},
        device_state=device_state,
        geofence_inside_since=geofence_inside_since or {},
        geofence_outside_since=geofence_outside_since or {},
        geofence_presence_episode=geofence_presence_episode or {},
        scheduled_outside_dwell_consumed_episode=(scheduled_outside_dwell_consumed_episode or {}),
        user_location_history=user_location_history or {},
    )


class _FakeKasaSwitch:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.host = host
        self.mac_address = None
        self.identifier = host
        self.preferred_label = label
        self._on = is_on

    @property
    def is_on(self) -> bool:
        return self._on


def _kasa_device_state(*switches: _FakeKasaSwitch) -> DeviceManagersState:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(switches)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


class _FakeSonosZone:
    def __init__(self, identifier: str, label: str, *, is_playing: bool | None) -> None:
        self.identifier = identifier
        self.rincon_uid = identifier
        self.mac_address = None
        self.preferred_label = label
        self.is_playing = is_playing


class _FakeVizioTv:
    def __init__(self, device_id: str, label: str, *, power: str) -> None:
        self.identifier = device_id
        self.mac_address = device_id if ":" in device_id else None
        self.preferred_label = label
        self._power = power

    def ui_power_state(self) -> str:
        return self._power


def _house_geofence() -> GeofenceOut:
    return GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )


def _henrique_inside_location() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def test_evaluate_rule_uses_triggered_by_for_dual_trigger_rule() -> None:
    now = datetime(2026, 6, 9, 23, 15, tzinfo=_TZ)
    geofence = _house_geofence()
    ctx = _ctx(
        now=now,
        geofences=(geofence,),
        user_locations={"henrique": _henrique_inside_location()},
    )
    rule = _evening_window_open_rule()
    edge_eval = evaluate_rule(
        rule,
        replace(ctx, triggered_by=RuleEvaluationCause.EDGE),
    )
    scheduled_eval = evaluate_rule(
        rule,
        replace(ctx, triggered_by=RuleEvaluationCause.SCHEDULED),
    )
    assert edge_eval.all_met is True
    assert scheduled_eval.all_met is True
    assert edge_eval.conditions[1].met is False
    assert scheduled_eval.conditions[1].met is True


def _dwell_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="both-home-dwell",
        label="Both home dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )


def test_after_sunset_met_in_evening_window() -> None:
    # June 9 2026 sunset at this lat/lon is ~8:27 PM local — use 9 PM.
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    result = evaluate_rule(_evening_rule(), _ctx(now=now))
    assert result.conditions[0].met is True
    assert "Evening window active" in result.conditions[0].detail


def test_after_sunset_not_met_midday() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    result = evaluate_rule(_evening_rule(), _ctx(now=now))
    assert result.conditions[0].met is False
    assert "Outside sunset" in result.conditions[0].detail


def _daylight_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[DaylightCondition(type="daylight")]),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="daylight-only",
        label="Daylight only",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="0 12 * * *",
    )


def test_daylight_met_between_sunrise_and_sunset() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    result = evaluate_rule(_daylight_rule(), _ctx(now=now))
    assert result.conditions[0].met is True
    assert "Daylight active" in result.conditions[0].detail


def test_daylight_not_met_at_night() -> None:
    now = datetime(2026, 6, 9, 22, 0, tzinfo=_TZ)
    result = evaluate_rule(_daylight_rule(), _ctx(now=now))
    assert result.conditions[0].met is False
    assert "Outside daylight hours" in result.conditions[0].detail


def test_effective_location_for_rule_ignores_usable_location_older_than_ten_minutes() -> None:
    now_epoch = datetime(2026, 6, 25, 17, 20, 0, tzinfo=_TZ).timestamp()
    latest = UserLocationOut(
        accuracy_m=120,
        lat=41.19,
        lon=-73.88,
        fix_at="2026-06-25T21:20:00Z",
        reported_at="2026-06-25T21:20:00Z",
        source="my-tracks",
    )
    old_good = UserLocationOut(
        accuracy_m=8,
        lat=41.19283,
        lon=-73.88230,
        fix_at="2026-06-25T21:03:36Z",
        reported_at="2026-06-25T21:03:36Z",
        source="my-tracks",
    )
    assert (
        _effective_location_for_rule(
            latest,
            (old_good,),
            min_accuracy_m=50,
            now_epoch=now_epoch,
        )
        is None
    )


def test_effective_location_for_rule_returns_latest_when_usable() -> None:
    now_epoch = datetime(2026, 6, 25, 17, 4, 0, tzinfo=_TZ).timestamp()
    latest = UserLocationOut(
        accuracy_m=8,
        lat=41.19283,
        lon=-73.88230,
        fix_at="2026-06-25T21:04:00Z",
        reported_at="2026-06-25T21:04:00Z",
        source="my-tracks",
    )
    assert (
        _effective_location_for_rule(
            latest,
            (),
            min_accuracy_m=50,
            now_epoch=now_epoch,
        )
        == latest
    )


def test_effective_location_for_rule_walks_back_within_ten_minutes() -> None:
    now_epoch = datetime(2026, 6, 25, 17, 10, 0, tzinfo=_TZ).timestamp()
    latest = UserLocationOut(
        accuracy_m=83,
        lat=41.19336,
        lon=-73.87992,
        fix_at="2026-06-25T21:10:00Z",
        reported_at="2026-06-25T21:10:00Z",
        source="my-tracks",
    )
    good = UserLocationOut(
        accuracy_m=8,
        lat=41.19283,
        lon=-73.88230,
        fix_at="2026-06-25T21:03:36Z",
        reported_at="2026-06-25T21:03:36Z",
        source="my-tracks",
    )
    assert (
        _effective_location_for_rule(
            latest,
            (good,),
            min_accuracy_m=50,
            now_epoch=now_epoch,
        )
        == good
    )


def test_users_inside_geofence_met_with_location() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    location = UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_rule(),
        _ctx(now=now, geofences=(geofence,), user_locations={"henrique": location}),
    )
    assert result.conditions[1].met is False
    assert "Henrique is inside House" in result.conditions[1].detail
    assert result.all_met is True


def test_presence_user_ids_for_rule_returns_only_users_inside_any_branch() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    kristen_location = UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    henrique_location = UserLocationOut(
        accuracy_m=20,
        lat=44.0,
        lon=-73.0,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    ctx = _ctx(
        now=now,
        geofences=(geofence,),
        user_locations={
            "henrique": henrique_location,
            "kristen": kristen_location,
        },
    )
    assert presence_user_ids_for_rule(_evening_arrival_any_rule(), ctx) == ("kristen",)


def test_presence_user_ids_for_condition_ignores_non_presence_types() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    ctx = _ctx(now=now, geofences=(), user_locations={})
    rule = _evening_rule()
    assert (
        _presence_user_ids_for_condition(
            AfterSunsetCondition(type="after_sunset", offset_minutes=0),
            rule,
            ctx,
        )
        == set()
    )
    assert (
        _presence_user_ids_for_condition(
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                state=DeviceConditionState.ON,
                devices=[
                    RuleConditionDeviceRefOut(
                        device_id="Garage",
                        family_id=DeviceFamilyId.KASA,
                    ),
                ],
            ),
            rule,
            ctx,
        )
        == set()
    )


def test_users_inside_geofence_ignores_low_accuracy() -> None:
    now = datetime(2026, 6, 9, 20, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    location = UserLocationOut(
        accuracy_m=120,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_rule(),
        _ctx(now=now, geofences=(geofence,), user_locations={"henrique": location}),
    )
    assert result.conditions[1].met is False
    assert "Ignored low-accuracy location" in result.conditions[1].detail


def test_edge_true_any_presence_reports_inside_outside_not_met() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    henrique_inside = UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    kristen_outside = UserLocationOut(
        accuracy_m=20,
        lat=44.417597,
        lon=-72.023842,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_arrival_any_rule(),
        _ctx(
            now=now,
            geofences=(geofence,),
            user_locations={
                "henrique": henrique_inside,
                "kristen": kristen_outside,
            },
        ),
    )
    any_row = result.conditions[1]
    assert any_row.met is False
    assert "Henrique is inside House" in any_row.detail
    assert "Kristen is outside House" in any_row.detail


def test_user_display_name_uses_roster_display_name() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    base_ctx = _ctx(now=now)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    result = evaluate_rule(
        _evening_arrival_any_rule(),
        RuleEvaluationContext(
            geofences=(geofence,),
            now=now,
            roster_user_id_lookup=build_roster_user_id_lookup(
                ["henrique", "kristen"],
            ),
            user_display_names={"henrique": "Henrique", "kristen": "Kristen"},
            user_locations={},
            sun=base_ctx.sun,
            timezone=_TZ,
        ),
    )
    assert "Henrique" in result.conditions[1].detail
    assert "Kristen" in result.conditions[1].detail


def test_users_geofence_unknown_user_reports_roster_miss() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    result = evaluate_rule(
        _evening_rule(),
        RuleEvaluationContext(
            geofences=(geofence,),
            now=now,
            roster_user_id_lookup=build_roster_user_id_lookup(["kristen"]),
            user_display_names={"kristen": "Kristen"},
            user_locations={},
            sun=compute_rules_sun_out(_SETTINGS, now=now),
            timezone=_TZ,
        ),
    )
    assert '"henrique": not in user roster' in result.conditions[1].detail


def test_users_inside_geofence_for_s_met_after_dwell_elapsed() -> None:
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    inside_since = now.timestamp() - 720.0
    result = evaluate_rule(
        _dwell_rule(),
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={("henrique", "house"): inside_since},
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.conditions[0].met is True
    assert "Everyone inside House for at least 10 min" in result.conditions[0].detail
    assert result.all_met is True


def _outside_dwell_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
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
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )


def _henrique_outside_location() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=20,
        lat=44.417597,
        lon=-72.023842,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


def test_users_outside_geofence_for_s_met_after_dwell() -> None:
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    outside_since = now.timestamp() - 1300.0
    result = evaluate_rule(
        _outside_dwell_rule(),
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_outside_since={("henrique", "house"): outside_since},
            user_locations={"henrique": _henrique_outside_location()},
        ),
    )
    assert result.conditions[0].met is True
    assert "Everyone outside House for at least 20 min" in result.conditions[0].detail
    assert result.all_met is True


def test_dwell_episode_blocks_fire_when_consumed() -> None:
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    outside_since = now.timestamp() - 1300.0
    ctx = _ctx(
        now=now,
        geofences=(_house_geofence(),),
        geofence_outside_since={("henrique", "house"): outside_since},
        geofence_presence_episode={("henrique", "house"): 2},
        scheduled_outside_dwell_consumed_episode={
            ("away-dwell", "henrique", "house"): 2,
        },
        user_locations={"henrique": _henrique_outside_location()},
    )
    assert evaluate_rule(_outside_dwell_rule(), ctx).all_met is True
    assert dwell_episode_blocks_fire(_outside_dwell_rule(), ctx)


def test_scheduled_dwell_episode_does_not_block_when_one_user_episode_unconsumed() -> None:
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    outside_since = now.timestamp() - 1300.0
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="away-dwell-both",
        label="Away dwell both",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    ctx = _ctx(
        now=now,
        geofences=(_house_geofence(),),
        geofence_outside_since={
            ("henrique", "house"): outside_since,
            ("kristen", "house"): outside_since,
        },
        geofence_presence_episode={
            ("henrique", "house"): 2,
            ("kristen", "house"): 0,
        },
        scheduled_outside_dwell_consumed_episode={
            ("away-dwell-both", "henrique", "house"): 0,
            ("away-dwell-both", "kristen", "house"): 0,
        },
        user_locations={
            "henrique": _henrique_outside_location(),
            "kristen": _henrique_outside_location(),
        },
    )
    assert dwell_episode_blocks_fire(rule, ctx) is False


def test_users_outside_geofence_for_s_met_with_outside_dwell_timer_despite_low_accuracy_latest() -> None:
    now = datetime(2026, 6, 25, 17, 20, 56, tzinfo=_TZ)
    outside_since = datetime(2026, 6, 25, 17, 0, 53, tzinfo=_TZ).timestamp()
    henrique_bad_latest = UserLocationOut(
        accuracy_m=83,
        connection_type="mobile",
        lat=41.19336,
        lon=-73.87992,
        fix_at="2026-06-25T21:20:33Z",
        reported_at="2026-06-25T21:20:33Z",
        source="my-tracks",
    )
    henrique_good_history = UserLocationOut(
        accuracy_m=8,
        connection_type="mobile",
        lat=41.19283,
        lon=-73.88230,
        fix_at="2026-06-25T21:15:36Z",
        reported_at="2026-06-25T21:15:36Z",
        source="my-tracks",
    )
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="away-dwell-both",
        label="Away dwell both",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_outside_since={
                ("henrique", "house"): outside_since,
                ("kristen", "house"): outside_since,
            },
            user_location_history={"henrique": (henrique_good_history,)},
            user_locations={
                "henrique": henrique_bad_latest,
                "kristen": _henrique_outside_location(),
            },
        ),
    )
    assert result.conditions[0].met is True


def test_users_outside_geofence_for_s_vetoed_when_walkback_shows_accurate_inside() -> None:
    now = datetime(2026, 6, 25, 17, 20, 56, tzinfo=_TZ)
    outside_since = datetime(2026, 6, 25, 17, 0, 53, tzinfo=_TZ).timestamp()
    henrique_bad_latest = UserLocationOut(
        accuracy_m=83,
        connection_type="mobile",
        lat=41.19336,
        lon=-73.87992,
        fix_at="2026-06-25T21:20:33Z",
        reported_at="2026-06-25T21:20:33Z",
        source="my-tracks",
    )
    henrique_inside_history = UserLocationOut(
        accuracy_m=4,
        connection_type="mobile",
        lat=41.19425,
        lon=-73.88863,
        fix_at="2026-06-25T21:15:36Z",
        reported_at="2026-06-25T21:15:36Z",
        source="my-tracks",
    )
    result = evaluate_rule(
        _outside_dwell_rule(),
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_outside_since={("henrique", "house"): outside_since},
            user_location_history={"henrique": (henrique_inside_history,)},
            user_locations={"henrique": henrique_bad_latest},
        ),
    )
    assert result.conditions[0].met is False
    assert "Henrique inside House" in result.conditions[0].detail


def test_users_inside_geofence_for_s_met_with_wifi_home_presence_low_accuracy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.rule_conditions.load_settings_location",
        lambda: SettingsLocationOut(
            lat=41.194072,
            lon=-73.8883254,
            timezone="America/New_York",
            wifi_home_geofence_id="house",
            wifi_home_presence_enabled=True,
        ),
    )
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    inside_since = now.timestamp() - 720.0
    kristen_wifi = UserLocationOut(
        accuracy_m=97,
        connection_type="w",
        lat=41.1941344,
        lon=-73.8882358,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="my-tracks",
    )
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="both-home-dwell",
        label="Both home dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={
                ("henrique", "house"): inside_since,
                ("kristen", "house"): inside_since,
            },
            user_locations={
                "henrique": _henrique_inside_location(),
                "kristen": kristen_wifi,
            },
        ),
    )
    assert result.conditions[0].met is True
    assert "Everyone inside House for at least 10 min" in result.conditions[0].detail


def test_users_inside_geofence_for_s_met_with_inside_dwell_timer_despite_low_accuracy() -> None:
    now = datetime(2026, 6, 9, 21, 12, tzinfo=_TZ)
    inside_since = now.timestamp() - 720.0
    kristen_mobile = UserLocationOut(
        accuracy_m=97,
        connection_type="m",
        lat=41.1941344,
        lon=-73.8882358,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="my-tracks",
    )
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["kristen"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="kristen-dwell",
        label="Kristen dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={("kristen", "house"): inside_since},
            user_locations={"kristen": kristen_mobile},
        ),
    )
    assert result.conditions[0].met is True
    assert "Everyone inside House for at least 10 min" in result.conditions[0].detail


_JUN22_KRISTEN_INSIDE_SINCE = datetime(2026, 6, 22, 20, 34, 41, tzinfo=_TZ).timestamp()
_JUN22_HENRIQUE_INSIDE_SINCE = datetime(2026, 6, 22, 21, 26, 24, tzinfo=_TZ).timestamp()
_JUN22_TICK_2130 = datetime(2026, 6, 22, 21, 30, 36, tzinfo=_TZ)
_JUN22_TICK_2140 = datetime(2026, 6, 22, 21, 40, 0, tzinfo=_TZ)
_JUN22_TICK_2145 = datetime(2026, 6, 22, 21, 45, 39, tzinfo=_TZ)


def _jun22_evening_lights_off_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique", "kristen"],
                ),
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.ON,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Front door lights",
                            family_id=DeviceFamilyId.KASA,
                        ),
                        RuleConditionDeviceRefOut(
                            device_id="Garage outside lights",
                            family_id=DeviceFamilyId.KASA,
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="evening-lights-off-both-home",
        label="Turn off arrival lights when both home 10+ min after sunset",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )


def _jun22_kristen_low_accuracy_location(
    *,
    connection_type: str,
) -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=97,
        connection_type=connection_type,
        lat=41.1941344,
        lon=-73.8882358,
        fix_at="2026-06-23T01:45:00Z",
        reported_at="2026-06-23T01:45:00Z",
        source="my-tracks",
    )


def _jun22_replay_ctx(
    now: datetime,
    *,
    connection_type: str = "w",
    lights_on: bool = False,
) -> RuleEvaluationContext:
    state = _kasa_device_state(
        _FakeKasaSwitch("10", "Front door lights", is_on=lights_on),
        _FakeKasaSwitch("11", "Garage outside lights", is_on=False),
    )
    return _ctx(
        now=now,
        device_state=state,
        geofences=(_house_geofence(),),
        geofence_inside_since={
            ("henrique", "house"): _JUN22_HENRIQUE_INSIDE_SINCE,
            ("kristen", "house"): _JUN22_KRISTEN_INSIDE_SINCE,
        },
        user_locations={
            "henrique": _henrique_inside_location(),
            "kristen": _jun22_kristen_low_accuracy_location(
                connection_type=connection_type,
            ),
        },
    )


def _patch_wifi_home_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.rule_conditions.load_settings_location",
        lambda: SettingsLocationOut(
            lat=41.194072,
            lon=-73.8883254,
            timezone="America/New_York",
            wifi_home_geofence_id="house",
            wifi_home_presence_enabled=True,
        ),
    )


def test_jun22_2145_dwell_accepts_kristen_with_inside_timer_without_wifi() -> None:
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2145, connection_type="m"),
    )
    dwell = result.conditions[1]
    assert dwell.met is True
    assert "Everyone inside House for at least 10 min" in dwell.detail


def test_jun22_2145_dwell_accepts_kristen_with_wifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wifi_home_settings(monkeypatch)
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2145, connection_type="w"),
    )
    dwell = result.conditions[1]
    assert dwell.met is True
    assert "Everyone inside House for at least 10 min" in dwell.detail


def test_jun22_2130_full_rule_unmet_henrique_dwell_short_with_wifi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wifi_home_settings(monkeypatch)
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2130, connection_type="w"),
    )
    dwell = result.conditions[1]
    assert dwell.met is False
    assert "Henrique inside 4 min 12 sec (need 10 min)" in dwell.detail
    assert result.all_met is False


def test_jun22_2145_full_rule_unmet_lights_off_with_wifi_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wifi_home_settings(monkeypatch)
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2145, connection_type="w", lights_on=False),
    )
    assert result.conditions[0].met is True
    assert result.conditions[1].met is True
    assert result.conditions[2].met is False
    assert "All off" in result.conditions[2].detail
    assert result.all_met is False


def test_jun22_2140_full_rule_met_lights_on_with_wifi_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wifi_home_settings(monkeypatch)
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2140, connection_type="w", lights_on=True),
    )
    assert result.all_met is True


def test_jun22_2145_full_rule_met_lights_on_with_wifi_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_wifi_home_settings(monkeypatch)
    result = evaluate_rule(
        _jun22_evening_lights_off_rule(),
        _jun22_replay_ctx(_JUN22_TICK_2145, connection_type="w", lights_on=True),
    )
    assert result.all_met is True


def test_users_inside_geofence_for_s_formats_subminute_dwell_in_seconds() -> None:
    now = datetime(2026, 6, 9, 21, 0, 35, tzinfo=_TZ)
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=30,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="short-dwell",
        label="Short dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    inside_since = now.timestamp() - 35.0
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={("henrique", "house"): inside_since},
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.conditions[0].met is True
    assert "Everyone inside House for at least 30 sec" in result.conditions[0].detail
    assert result.conditions[0].label == "Inside House 30 sec+ (Henrique)"


def test_users_inside_geofence_for_s_formats_non_minute_aligned_need() -> None:
    now = datetime(2026, 6, 9, 21, 1, 5, tzinfo=_TZ)
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=61,
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="odd-dwell",
        label="Odd dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    inside_since = now.timestamp() - 65.0
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={("henrique", "house"): inside_since},
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.conditions[0].met is True
    assert "1 min 1 sec" in result.conditions[0].detail


def test_users_inside_geofence_for_s_unmet_before_dwell_elapsed() -> None:
    now = datetime(2026, 6, 9, 21, 5, tzinfo=_TZ)
    inside_since = now.timestamp() - 300.0
    result = evaluate_rule(
        _dwell_rule(),
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={("henrique", "house"): inside_since},
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.conditions[0].met is False
    assert "Henrique inside 5 min (need 10 min)" in result.conditions[0].detail


def test_users_inside_geofence_for_s_reports_user_outside() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    kristen_outside = UserLocationOut(
        accuracy_m=20,
        lat=44.417597,
        lon=-72.023842,
        fix_at="2026-06-09T23:00:00Z",
        reported_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    rule = RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceForSCondition(
                    type="users_inside_geofence_for_s",
                    geofence_id="house",
                    min_inside_s=600,
                    user_ids=["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="both-home-dwell",
        label="Both home dwell",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )
    inside_since = now.timestamp() - 900.0
    result = evaluate_rule(
        rule,
        _ctx(
            now=now,
            geofences=(_house_geofence(),),
            geofence_inside_since={
                ("henrique", "house"): inside_since,
                ("kristen", "house"): inside_since,
            },
            user_locations={
                "henrique": _henrique_inside_location(),
                "kristen": kristen_outside,
            },
        ),
    )
    assert result.conditions[0].met is False
    assert "Kristen outside" in result.conditions[0].detail


def _device_state_rule(
    condition: (DevicesAllInStateCondition | DevicesAnyInStateCondition),
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[condition]),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="device-state",
        label="Device state",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.SCHEDULED],
        schedule_cron="*/10 * * * *",
    )


def _media_device_state(
    *,
    sonos_zones: tuple[_FakeSonosZone, ...] = (),
    vizio_tvs: tuple[_FakeVizioTv, ...] = (),
) -> DeviceManagersState:
    kasa_mgr = MagicMock(spec=KasaDeviceManager)
    kasa_mgr.switches = ()
    sonos_mgr = MagicMock(spec=SonosDeviceManager)
    sonos_mgr.players = sonos_zones
    vizio_mgr = MagicMock(spec=VizioDeviceManager)
    vizio_mgr.tvs = vizio_tvs
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=kasa_mgr,
        sonos_mgr=sonos_mgr,
        tailwind_mgr=None,
        vizio_mgr=vizio_mgr,
    )


class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.mac_address = None
        self.door_key = self.identifier
        self.preferred_label = label
        self.is_open = is_open


def _tailwind_device_state(*doors: _FakeTailwindDoor) -> DeviceManagersState:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    return DeviceManagersState(
        androidtv_mgr=None,
        ep1_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=mgr,
        vizio_mgr=None,
    )


def test_devices_any_open_reports_open_tailwind_door() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _tailwind_device_state(
        _FakeTailwindDoor("door-left", "Left", is_open=True),
        _FakeTailwindDoor("door-right", "Right", is_open=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OPEN,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Left",
                    family_id=DeviceFamilyId.TAILWIND,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Right",
                    family_id=DeviceFamilyId.TAILWIND,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Open: Left" in result.conditions[0].detail


def test_devices_any_on_met_when_one_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "On: Front door lights" in result.conditions[0].detail


def test_devices_any_on_met_when_one_on_and_another_missing() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "On: Front door lights" in result.conditions[0].detail


def test_devices_any_on_short_circuits_after_first_on_device() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    call_count = 0
    original = cached_kasa_is_on

    def counting_cached_kasa_is_on(
        device_state: DeviceManagersState,
        device_id: str,
    ) -> bool | None:
        nonlocal call_count
        call_count += 1
        return original(device_state, device_id)

    with patch(
        "app.rule_conditions.cached_kasa_is_on",
        side_effect=counting_cached_kasa_is_on,
    ):
        result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.conditions[0].met is True
    assert call_count == 1


def test_devices_any_on_unmet_when_all_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=False),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.conditions[0].met is False
    assert "All off" in result.conditions[0].detail


def test_devices_any_on_unmet_when_discovery_not_ready() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=None))
    assert result.conditions[0].met is False
    assert result.conditions[0].detail == "discovery not ready"


def test_devices_any_on_met_when_sonos_zone_playing() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _media_device_state(
        sonos_zones=(
            _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=True),
            _FakeSonosZone("RINCON_BBBB", "Living Room", is_playing=False),
        ),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Kitchen",
                    family_id=DeviceFamilyId.SONOS,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Living Room",
                    family_id=DeviceFamilyId.SONOS,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "On: Kitchen" in result.conditions[0].detail


def test_devices_any_on_met_when_vizio_tv_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _media_device_state(
        vizio_tvs=(_FakeVizioTv("192.168.1.10", "Kitchen TV", power="on"),),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Kitchen TV",
                    family_id=DeviceFamilyId.VIZIO,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "On: Kitchen TV" in result.conditions[0].detail


def test_devices_any_off_met_when_one_switch_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Off: Garage outside lights" in result.conditions[0].detail


def test_devices_any_off_met_when_one_off_and_another_missing() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Off: Garage outside lights" in result.conditions[0].detail


def test_devices_any_off_short_circuits_after_first_off_device() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    call_count = 0
    original = cached_kasa_is_on

    def counting_cached_kasa_is_on(
        device_state: DeviceManagersState,
        device_id: str,
    ) -> bool | None:
        nonlocal call_count
        call_count += 1
        return original(device_state, device_id)

    with patch(
        "app.rule_conditions.cached_kasa_is_on",
        side_effect=counting_cached_kasa_is_on,
    ):
        result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.conditions[0].met is True
    assert call_count == 2


def test_devices_any_off_unmet_when_all_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=True),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.conditions[0].met is False
    assert "All on" in result.conditions[0].detail


def test_devices_any_off_unmet_when_discovery_not_ready() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=None))
    assert result.conditions[0].met is False
    assert result.conditions[0].detail == "discovery not ready"


def test_devices_any_off_met_when_sonos_zone_paused() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _media_device_state(
        sonos_zones=(
            _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=True),
            _FakeSonosZone("RINCON_BBBB", "Living Room", is_playing=False),
        ),
    )
    rule = _device_state_rule(
        DevicesAnyInStateCondition(
            type="devices_any_in_state",
            state=DeviceConditionState.OFF,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Kitchen",
                    family_id=DeviceFamilyId.SONOS,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Living Room",
                    family_id=DeviceFamilyId.SONOS,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert result.conditions[0].met is True
    assert "Off: Living Room" in result.conditions[0].detail


def test_devices_all_on_met_when_every_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=True),
    )
    rule = _device_state_rule(
        DevicesAllInStateCondition(
            type="devices_all_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.all_met is True
    assert "All on" in result.conditions[0].detail


def test_devices_all_on_unmet_when_one_switch_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAllInStateCondition(
            type="devices_all_in_state",
            state=DeviceConditionState.ON,
            devices=[
                RuleConditionDeviceRefOut(
                    device_id="Front door lights",
                    family_id=DeviceFamilyId.KASA,
                ),
                RuleConditionDeviceRefOut(
                    device_id="Garage outside lights",
                    family_id=DeviceFamilyId.KASA,
                ),
            ],
        ),
    )
    result = evaluate_rule(rule, _ctx(now=now, device_state=state))
    assert result.conditions[0].met is False
    assert "Off: Garage outside lights" in result.conditions[0].detail


def test_build_rules_status_from_example_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    status = build_rules_status(cache_path=tmp_path / "unused.sqlite")
    assert len(status.rules) == 13
    assert status.sun.sunset_at.endswith("Z")
    assert status.evaluator.last_run_at is not None
    dark = next(row for row in status.rules if row.id == "daylight-dark-house-lights-on")
    assert dark.enabled is False
    expected_label = next(
        rule.label for rule in load_automation_rules_bundle().rules if rule.id == "daylight-dark-house-lights-on"
    )
    assert dark.label == expected_label
