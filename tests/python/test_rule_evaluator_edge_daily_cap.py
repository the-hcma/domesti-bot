"""Hermetic tests for edge_true rules with fire_once_per_local_day."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    AnyConditionsCondition,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    RulesSunOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


class _FakeKasa:
    def __init__(self, host: str, label: str) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")


def _evening_interior_edge_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-25,
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
                    ],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Kitchen lamp",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-interior",
        label="Evening interior",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        trigger="edge_true",
    )


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = KasaDeviceManager.__new__(KasaDeviceManager)
    mgr._device_name_to_device = {device.preferred_label: device for device in devices}  # type: ignore[assignment]
    return mgr


def _mock_sun_for_nov_14_2023() -> RulesSunOut:
    return RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )


def _move_henrique_inside_house(fixture: dict[str, object]) -> None:
    clock = fixture["clock"]
    assert isinstance(clock, dict)
    db = fixture["db"]
    assert isinstance(db, Path)
    clock["now"] = float(clock["now"]) + 60.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _write_bundle(path: Path, rule: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def evening_interior_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_interior_edge_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.rule_evaluator.compute_rules_sun_out",
        lambda *args, **kwargs: _mock_sun_for_nov_14_2023(),
    )

    tz = ZoneInfo("America/New_York")
    anchor_local = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(
        tz,
    ) - timedelta(minutes=25)
    clock = {"now": (anchor_local + timedelta(minutes=5)).timestamp()}

    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Test",
                last_name="",
                display_name="Test",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    replace_geofences(
        db,
        [
            GeofenceRecord(
                geofence_id="house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.888325,
                radius_m=250,
                enabled=True,
                owntracks_rid=None,
            ),
        ],
    )
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=clock["now"] - 400.0,
            reported_at=clock["now"] - 400.0,
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    device = _FakeKasa("192.168.1.10", "Kitchen lamp")
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    return {
        "anchor_local": anchor_local,
        "clock": clock,
        "db": db,
        "device": device,
        "evaluator": evaluator,
        "tz": tz,
    }


@pytest.mark.asyncio
async def test_edge_rule_armed_during_evening_window_fires_on_enter(
    evening_interior_fixture: dict[str, object],
) -> None:
    """Rule is armed between sunset-25 and midnight; home enter fires once."""
    _move_henrique_inside_house(evening_interior_fixture)
    evaluator = evening_interior_fixture["evaluator"]
    device = evening_interior_fixture["device"]
    assert isinstance(evaluator, RuleEvaluator)
    assert isinstance(device, _FakeKasa)
    await evaluator.on_location_update("henrique")
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_edge_rule_disarmed_after_evening_window_closes_at_midnight(
    evening_interior_fixture: dict[str, object],
) -> None:
    """Rule disarms at local midnight; home enter after midnight must not fire."""
    clock = evening_interior_fixture["clock"]
    tz = evening_interior_fixture["tz"]
    anchor_local = evening_interior_fixture["anchor_local"]
    assert isinstance(clock, dict)
    assert isinstance(tz, ZoneInfo)
    assert isinstance(anchor_local, datetime)
    local_midnight = datetime(
        anchor_local.year,
        anchor_local.month,
        anchor_local.day,
        tzinfo=tz,
    ) + timedelta(days=1)
    clock["now"] = (local_midnight + timedelta(minutes=30)).timestamp()
    _move_henrique_inside_house(evening_interior_fixture)
    evaluator = evening_interior_fixture["evaluator"]
    device = evening_interior_fixture["device"]
    assert isinstance(evaluator, RuleEvaluator)
    assert isinstance(device, _FakeKasa)
    await evaluator.on_location_update("henrique")
    assert device.calls == []


@pytest.mark.asyncio
async def test_edge_rule_disarmed_before_evening_window_opens(
    evening_interior_fixture: dict[str, object],
) -> None:
    """Rule is not armed before sunset-25; home enter must not fire."""
    clock = evening_interior_fixture["clock"]
    anchor_local = evening_interior_fixture["anchor_local"]
    assert isinstance(clock, dict)
    assert isinstance(anchor_local, datetime)
    clock["now"] = (anchor_local - timedelta(minutes=5)).timestamp()
    _move_henrique_inside_house(evening_interior_fixture)
    evaluator = evening_interior_fixture["evaluator"]
    device = evening_interior_fixture["device"]
    assert isinstance(evaluator, RuleEvaluator)
    assert isinstance(device, _FakeKasa)
    await evaluator.on_location_update("henrique")
    assert device.calls == []


@pytest.mark.asyncio
async def test_edge_rule_fire_once_per_local_day_skips_second_enter_same_day(
    evening_interior_fixture: dict[str, object],
) -> None:
    _move_henrique_inside_house(evening_interior_fixture)
    evaluator = evening_interior_fixture["evaluator"]
    device = evening_interior_fixture["device"]
    clock = evening_interior_fixture["clock"]
    db = evening_interior_fixture["db"]
    assert isinstance(evaluator, RuleEvaluator)
    assert isinstance(device, _FakeKasa)
    assert isinstance(clock, dict)
    assert isinstance(db, Path)

    await evaluator.on_location_update("henrique")
    assert device.calls == ["on"]

    clock["now"] = float(clock["now"]) + 600.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=clock["now"],
            reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    _move_henrique_inside_house(evening_interior_fixture)
    await evaluator.on_location_update("henrique")
    assert device.calls == ["on"]
