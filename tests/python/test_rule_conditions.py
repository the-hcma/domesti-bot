"""Unit tests for server-side automation rule condition evaluation."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    AnyConditionsCondition,
    DevicesAllOnCondition,
    DevicesAnyOnCondition,
    GeofenceOut,
    RuleConditionDeviceRefOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    UsersInsideGeofenceForSCondition,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.device_enums import DeviceFamilyId
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rule_validation import build_roster_user_id_lookup
from app.rules_status import build_rules_status

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
        notification_email=None,
        notify_on_fire=False,
        trigger="edge_true",
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
        notification_email=None,
        notify_on_fire=False,
        trigger="edge_true",
    )


def _ctx(
    *,
    now: datetime,
    device_state: DeviceManagersState | None = None,
    geofences: tuple[GeofenceOut, ...] = (),
    geofence_inside_since: dict[tuple[str, str], float] | None = None,
    user_locations: dict[str, UserLocationOut] | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique", "kristen": "Kristen"}
    return RuleEvaluationContext(
        device_state=device_state,
        geofence_inside_since=geofence_inside_since or {},
        geofences=geofences,
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(
            list(user_display_names.keys()),
        ),
        user_display_names=user_display_names,
        user_locations=user_locations or {},
        sun=sun,
        timezone=_TZ,
    )


class _FakeKasaSwitch:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
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
        self.preferred_label = label
        self.is_playing = is_playing


class _FakeVizioTv:
    def __init__(self, device_id: str, label: str, *, power: str) -> None:
        self.identifier = device_id
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
        received_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )


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
        notification_email=None,
        notify_on_fire=False,
        trigger="scheduled",
        schedule_cron="*/15 * * * *",
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
        received_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_rule(),
        _ctx(now=now, geofences=(geofence,), user_locations={"henrique": location}),
    )
    assert result.conditions[1].met is False
    assert "Henrique is inside House" in result.conditions[1].detail
    assert result.all_met is True


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
        received_at="2026-06-09T23:00:00Z",
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
        received_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    kristen_outside = UserLocationOut(
        accuracy_m=20,
        lat=44.417597,
        lon=-72.023842,
        received_at="2026-06-09T23:00:00Z",
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
        notification_email=None,
        notify_on_fire=False,
        trigger="scheduled",
        schedule_cron="*/15 * * * *",
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
        notification_email=None,
        notify_on_fire=False,
        trigger="scheduled",
        schedule_cron="*/15 * * * *",
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
        received_at="2026-06-09T23:00:00Z",
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
        notification_email=None,
        notify_on_fire=False,
        trigger="scheduled",
        schedule_cron="*/15 * * * *",
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
    condition: DevicesAnyOnCondition | DevicesAllOnCondition,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[condition]),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="device-state",
        label="Device state",
        min_location_accuracy_m=50,
        notification_email=None,
        notify_on_fire=False,
        trigger="scheduled",
        schedule_cron="*/15 * * * *",
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
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=kasa_mgr,
        sonos_mgr=sonos_mgr,
        tailwind_mgr=None,
        vizio_mgr=vizio_mgr,
    )


def test_devices_any_on_met_when_one_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyOnCondition(
            type="devices_any_on",
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
        DevicesAnyOnCondition(
            type="devices_any_on",
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
    assert "not found: Garage outside lights" in result.conditions[0].detail


def test_devices_any_on_unmet_when_all_off() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=False),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=False),
    )
    rule = _device_state_rule(
        DevicesAnyOnCondition(
            type="devices_any_on",
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
        DevicesAnyOnCondition(
            type="devices_any_on",
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
        DevicesAnyOnCondition(
            type="devices_any_on",
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
        DevicesAnyOnCondition(
            type="devices_any_on",
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


def test_devices_all_on_met_when_every_switch_on() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    state = _kasa_device_state(
        _FakeKasaSwitch("192.168.1.10", "Front door lights", is_on=True),
        _FakeKasaSwitch("192.168.1.11", "Garage outside lights", is_on=True),
    )
    rule = _device_state_rule(
        DevicesAllOnCondition(
            type="devices_all_on",
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
        DevicesAllOnCondition(
            type="devices_all_on",
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
    assert len(status.rules) == 5
    assert status.sun.sunset_at.endswith("Z")
    assert status.evaluator.last_run_at is not None
