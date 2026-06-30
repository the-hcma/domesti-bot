"""Hermetic tests for astronomical scheduled rule evaluation."""

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
from app.astronomical_schedule import schedule_materialized_for_date
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.rule_evaluator import RuleEvaluator
from app.rule_fire_state_store import list_rule_fire_states


class _FakeKasa:
    def __init__(self, host: str, label: str) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = KasaDeviceManager.__new__(KasaDeviceManager)
    mgr._device_name_to_device = {device.preferred_label: device for device in devices}  # type: ignore[assignment]
    return mgr


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


def _evening_anchor_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-15,
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
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-anchor",
        label="Evening anchor",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        trigger="scheduled",
    )


def _mock_sun_for_nov_14_2023() -> RulesSunOut:
    return RulesSunOut(
        is_dark=False,
        sunrise_at="2023-11-14T11:30:00Z",
        sunset_at="2023-11-14T22:30:00Z",
    )


@pytest.fixture
def astronomical_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, object]:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_anchor_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.rule_evaluator.compute_rules_sun_out",
        lambda *args, **kwargs: _mock_sun_for_nov_14_2023(),
    )

    tz = ZoneInfo("America/New_York")
    anchor_local = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(
        tz,
    ) - timedelta(minutes=15)
    clock = {"now": (anchor_local - timedelta(minutes=2)).timestamp()}

    from app.presence_store import UserLocationRecord, upsert_user_location
    from app.location_history_retention import default_location_history_retention
    from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

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
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    device = _FakeKasa("192.168.1.10", "Garage")
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
async def test_astronomical_rule_materializes_cron_on_boot(
    astronomical_fixture: dict[str, object],
) -> None:
    evaluator = astronomical_fixture["evaluator"]
    assert isinstance(evaluator, RuleEvaluator)
    cron = evaluator.effective_schedule_cron_for_rule("evening-anchor")
    assert cron is not None
    assert cron.count(" ") == 4
    next_at = evaluator.next_evaluate_at_for_rule("evening-anchor")
    assert next_at is not None


@pytest.mark.asyncio
async def test_astronomical_rule_fires_once_at_materialized_anchor(
    astronomical_fixture: dict[str, object],
) -> None:
    clock = astronomical_fixture["clock"]
    assert isinstance(clock, dict)
    anchor_local = astronomical_fixture["anchor_local"]
    assert isinstance(anchor_local, datetime)
    evaluator = astronomical_fixture["evaluator"]
    assert isinstance(evaluator, RuleEvaluator)
    device = astronomical_fixture["device"]
    assert isinstance(device, _FakeKasa)

    await evaluator._evaluate_scheduled_rules()
    assert device.calls == []

    clock["now"] = anchor_local.timestamp()
    await evaluator._evaluate_scheduled_rules()
    assert device.calls == ["on"]

    clock["now"] = anchor_local.timestamp() + 120.0
    await evaluator._evaluate_scheduled_rules()
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_astronomical_schedule_persists_and_restores_on_restart(
    astronomical_fixture: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = astronomical_fixture["clock"]
    assert isinstance(clock, dict)
    db = astronomical_fixture["db"]
    assert isinstance(db, Path)
    evaluator = astronomical_fixture["evaluator"]
    assert isinstance(evaluator, RuleEvaluator)
    tz = astronomical_fixture["tz"]
    assert isinstance(tz, ZoneInfo)

    next_at = evaluator.next_evaluate_at_for_rule("evening-anchor")
    cron = evaluator.effective_schedule_cron_for_rule("evening-anchor")
    assert next_at is not None
    assert cron is not None

    rows = list_rule_fire_states(db)
    record = rows["evening-anchor"]
    assert record.next_evaluate_at == next_at
    assert record.effective_schedule_cron == cron
    assert record.schedule_materialized_for == schedule_materialized_for_date(
        datetime.fromtimestamp(clock["now"], tz=tz).date(),
    )

    restarted = RuleEvaluator(
        cache_path=db,
        device_state_getter=evaluator._device_state_getter,
        now_fn=lambda: clock["now"],
    )
    assert restarted.next_evaluate_at_for_rule("evening-anchor") == next_at
    assert restarted.effective_schedule_cron_for_rule("evening-anchor") == cron


def _evening_anchor_repeat_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=-15,
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
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-anchor-repeat",
        label="Evening anchor repeat",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="*/10 * * * *",
        trigger="scheduled",
    )


@pytest.mark.asyncio
async def test_astronomical_repeat_rule_fires_after_anchor_when_home_arrives_later(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_anchor_repeat_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.rule_evaluator.compute_rules_sun_out",
        lambda *args, **kwargs: _mock_sun_for_nov_14_2023(),
    )

    tz = ZoneInfo("America/New_York")
    anchor_local = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(
        tz,
    ) - timedelta(minutes=15)
    clock = {"now": anchor_local.timestamp()}

    from app.presence_store import UserLocationRecord, upsert_user_location
    from app.location_history_retention import default_location_history_retention
    from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

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
    device = _FakeKasa("192.168.1.10", "Garage")
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

    await evaluator._evaluate_scheduled_rules()
    assert device.calls == []

    from croniter import croniter

    next_tick = croniter("*/10 * * * *", anchor_local).get_next(datetime)
    if next_tick.tzinfo is None:
        next_tick = next_tick.replace(tzinfo=tz)
    clock["now"] = next_tick.timestamp()
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    await evaluator._evaluate_scheduled_rules()
    assert device.calls == ["on"]
