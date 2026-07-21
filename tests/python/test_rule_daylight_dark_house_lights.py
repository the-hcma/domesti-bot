"""Composition tests for daylight + dark EP1 lux + someone home (#554)."""

from __future__ import annotations

import argparse
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.api.schemas import (
    AnyConditionsCondition,
    DaylightCondition,
    DevicesAnyInStateCondition,
    Ep1ReadingCompareCondition,
    GeofenceOut,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    SettingsLocationOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    Ep1ReadingComparison,
    Ep1ReadingMetric,
    RuleDeviceActionType,
    RuleTrigger,
)
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_conditions import RuleEvaluationContext, compute_rules_sun_out, evaluate_rule
from app.rule_validation import build_roster_user_id_lookup


def test_daylight_dark_house_lights_met_when_dark_daytime_and_someone_home() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=40.0),
        switches=_target_switches(all_on=False),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.all_met is True
    assert all(row.met for row in result.conditions)


def test_daylight_dark_house_lights_unmet_at_night() -> None:
    now = datetime(2026, 6, 9, 22, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=40.0),
        switches=_target_switches(all_on=False),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.all_met is False
    assert result.conditions[0].met is False
    assert "Outside daylight hours" in result.conditions[0].detail


def test_daylight_dark_house_lights_unmet_when_all_target_lights_already_on() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=40.0),
        switches=_target_switches(all_on=True),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.all_met is False
    lights = next(row for row in result.conditions if row.condition.type == "devices_any_in_state")
    assert lights.met is False
    assert "All on" in lights.detail


def test_daylight_dark_house_lights_unmet_when_bright() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=200.0),
        switches=_target_switches(all_on=False),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.all_met is False
    illuminance = next(row for row in result.conditions if row.condition.type == "ep1_reading_compare")
    assert illuminance.met is False
    assert "not below" in illuminance.detail


def test_daylight_dark_house_lights_unmet_when_nobody_home() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=40.0),
        switches=_target_switches(all_on=False),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={},
        ),
    )
    assert result.all_met is False
    presence = next(row for row in result.conditions if row.condition.type == "any")
    assert presence.met is False


def test_daylight_dark_house_lights_unmet_when_reading_unavailable() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    state = _device_state(
        ep1=_FakeEp1Sensor(_WINDOW_EP1_MAC, "Window EP1", illuminance_lx=None),
        switches=_target_switches(all_on=False),
    )
    result = evaluate_rule(
        _dark_house_lights_rule(),
        _ctx(
            now=now,
            device_state=state,
            geofences=(_house_geofence(),),
            user_locations={"henrique": _henrique_inside_location()},
        ),
    )
    assert result.all_met is False
    illuminance = next(row for row in result.conditions if row.condition.type == "ep1_reading_compare")
    assert illuminance.met is False
    assert "reading unavailable" in illuminance.detail


class _FakeEp1Sensor:
    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        illuminance_lx: float | None,
    ) -> None:
        self.identifier = identifier
        self.illuminance_lx = illuminance_lx
        self.mac_address = identifier
        self.preferred_label = label
        self.humidity_pct = None
        self.temperature_c = None


class _FakeKasaSwitch:
    def __init__(self, mac: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = mac
        self._on = is_on
        self.host = mac
        self.identifier = mac
        self.mac_address = mac
        self.preferred_label = label

    @property
    def is_on(self) -> bool:
        return self._on


_ILLUMINANCE_THRESHOLD_LX = 80.0
_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TARGET_LIGHTS: tuple[tuple[str, str], ...] = (
    ("02:00:00:00:00:05", "Basement leds"),
    ("02:00:00:00:00:06", "Basement lamp"),
    ("02:00:00:00:00:07", "Hallway lights"),
)
_TZ = ZoneInfo("America/New_York")
_WINDOW_EP1_MAC = "02:00:00:00:00:21"


def _ctx(
    *,
    now: datetime,
    device_state: DeviceManagersState | None = None,
    geofences: tuple[GeofenceOut, ...] = (),
    user_locations: dict[str, UserLocationOut] | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    user_display_names = {"henrique": "Henrique", "kristen": "Kristen"}
    return RuleEvaluationContext(
        device_state=device_state,
        geofences=geofences,
        now=now,
        roster_user_id_lookup=build_roster_user_id_lookup(
            list(user_display_names.keys()),
        ),
        sun=sun,
        timezone=_TZ,
        user_display_names=user_display_names,
        user_locations=user_locations or {},
    )


def _dark_house_lights_rule() -> RuleOut:
    device_refs = [
        RuleConditionDeviceRefOut(
            device_id=mac,
            display_name=label,
            family_id=DeviceFamilyId.KASA,
        )
        for mac, label in _TARGET_LIGHTS
    ]
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DaylightCondition(type="daylight"),
                Ep1ReadingCompareCondition(
                    type="ep1_reading_compare",
                    comparison=Ep1ReadingComparison.BELOW,
                    device=RuleConditionDeviceRefOut(
                        device_id=_WINDOW_EP1_MAC,
                        display_name="Window EP1",
                        family_id=DeviceFamilyId.EP1,
                    ),
                    metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                    threshold=_ILLUMINANCE_THRESHOLD_LX,
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
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    devices=device_refs,
                    state=DeviceConditionState.OFF,
                ),
            ],
        ),
        cooldown_s=900,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_ON,
                device_id=mac,
                display_name=label,
                family_id=DeviceFamilyId.KASA,
            )
            for mac, label in _TARGET_LIGHTS
        ],
        enabled=True,
        id="daylight-dark-house-lights-on",
        label="Turn on interior lights when house is dark during daylight",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="*/5 * * * *",
        triggers=[RuleTrigger.SCHEDULED],
    )


def _device_state(
    *,
    ep1: _FakeEp1Sensor,
    switches: tuple[_FakeKasaSwitch, ...],
) -> DeviceManagersState:
    ep1_mgr = MagicMock(spec=Ep1DeviceManager)
    ep1_mgr.devices = (ep1,)
    kasa_mgr = MagicMock(spec=KasaDeviceManager)
    kasa_mgr.switches = switches
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        ep1_mgr=ep1_mgr,
        kasa_mgr=kasa_mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def _henrique_inside_location() -> UserLocationOut:
    return UserLocationOut(
        accuracy_m=8,
        fix_at="2026-06-09T16:00:00Z",
        lat=41.194072,
        lon=-73.8883254,
        reported_at="2026-06-09T16:00:00Z",
        source="my-tracks",
    )


def _house_geofence() -> GeofenceOut:
    return GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.8883254,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )


def _target_switches(*, all_on: bool) -> tuple[_FakeKasaSwitch, ...]:
    return tuple(_FakeKasaSwitch(mac, label, is_on=all_on) for mac, label in _TARGET_LIGHTS)
