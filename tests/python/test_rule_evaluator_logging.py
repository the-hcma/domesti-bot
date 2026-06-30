"""Hermetic tests for automation rule evaluator diagnostic logging."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

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
from app.rule_actions import RuleNotificationEmailOutcome
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


def _kasa_mgr(device: _FakeKasa) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = (device,)
    return cast(KasaDeviceManager, mgr)


def _scheduled_anyone_home_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
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
        cooldown_s=cooldown_s,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        id="scheduled-anyone-home",
        label="Scheduled anyone home",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        schedule_cron="* * * * *",
        trigger="scheduled",
    )


def _seed_presence_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    reported_at: float,
    fix_at: float | None = None,
) -> None:
    fix_epoch = reported_at if fix_at is None else fix_at
    replace_users(
        cache_path,
        [
            UserRecord(
                user_id=user_id,
                first_name="Test",
                last_name="",
                display_name="Test",
                tracking_device_label="Phone",
                enabled=True,
            ),
        ],
    )
    replace_geofences(
        cache_path,
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
        cache_path,
        UserLocationRecord(
            user_id=user_id,
            lat=lat,
            lon=lon,
            accuracy_m=20,
            fix_at=fix_epoch, reported_at=reported_at,
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _arrive_home_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
        enabled=True,
        id="arrive-home",
        label="Arrive home",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        trigger="edge_true",
    )


def _formatted_logger_calls(info_mock: MagicMock) -> list[str]:
    messages: list[str] = []
    for call in info_mock.call_args_list:
        args = call.args
        if not args:
            continue
        fmt = str(args[0])
        if len(args) > 1:
            messages.append(fmt % args[1:])
        else:
            messages.append(fmt)
    return messages


def _info_messages_matching(info_mock: MagicMock, needle: str) -> list[str]:
    return [
        message for message in _formatted_logger_calls(info_mock) if needle in message
    ]


@pytest.mark.asyncio
async def test_fired_log_includes_user_transitions_and_conditions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule(cooldown_s=300))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0, reported_at=clock["now"] - 400.0,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )

    clock["now"] += 60.0
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

    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    fired = _info_messages_matching(info_mock, "fired rule_id=arrive-home")
    assert fired
    message = fired[0]
    assert "user_ids=henrique" in message
    assert "email=disabled" in message
    assert "house:entered" in message
    assert "conditions=Presence at House (Test): Test is inside House" in message
    assert "=unmet" not in message


@pytest.mark.asyncio
async def test_debounced_geofence_enter_logs_suppressed_at_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=41.194085,
        lon=-73.888365,
        fix_at=clock["now"], reported_at=clock["now"],
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )

    clock["now"] += 30.0
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=44.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    await evaluator.on_location_update("henrique")

    clock["now"] += 60.0
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

    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    suppressed = _info_messages_matching(info_mock, "geofence enter suppressed")
    assert suppressed
    assert "user_id=henrique" in suppressed[0]
    assert "geofence_id=house" in suppressed[0]


@pytest.mark.asyncio
async def test_conditions_not_met_logs_skip_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    sunset_rule = _arrive_home_rule(cooldown_s=0).model_copy(
        update={
            "conditions": RuleConditionsOut(
                all=[
                    UsersInsideGeofenceCondition(
                        type="users_inside_geofence",
                        geofence_id="house",
                        user_ids=["henrique"],
                    ),
                    AfterSunsetCondition(
                        type="after_sunset",
                        offset_minutes=0,
                        window_end="midnight",
                    ),
                ],
            ),
        },
    )
    _write_bundle(bundle, sunset_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.rule_evaluator.compute_rules_sun_out",
        lambda *args, **kwargs: RulesSunOut(
            is_dark=False,
            sunrise_at="2026-06-01T09:00:00-04:00",
            sunset_at="2026-06-01T20:00:00-04:00",
        ),
    )

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        fix_at=clock["now"] - 400.0, reported_at=clock["now"] - 400.0,
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )

    clock["now"] += 60.0
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

    with patch("app.rule_evaluator._LOGGER.info") as info_mock:
        await evaluator.on_location_update("henrique")

    skipped = [
        message
        for message in _info_messages_matching(info_mock, "skipped rule_id=arrive-home")
        if "reason=conditions_not_met" in message
    ]
    assert skipped
    assert device.calls == []


@pytest.mark.asyncio
async def test_scheduled_fire_logs_presence_user_ids_and_email_outcome(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _scheduled_anyone_home_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Phone",
                enabled=True,
            ),
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
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
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="kristen",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            fix_at=clock["now"], reported_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )
    device = _FakeKasa("192.168.1.10", "Garage")
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: DeviceManagersState(
            kasa_mgr=_kasa_mgr(device),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        ),
        now_fn=lambda: clock["now"],
    )
    runtime = evaluator._rule_state["scheduled-anyone-home"]
    runtime.next_evaluate_at = clock["now"] - 1.0

    with (
        patch(
            "app.rule_evaluator.send_rule_notification_email",
            return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
        ),
        patch("app.rule_evaluator._LOGGER.info") as info_mock,
    ):
        await evaluator._evaluate_scheduled_rules()

    fired = _info_messages_matching(info_mock, "fired rule_id=scheduled-anyone-home")
    assert fired
    message = fired[0]
    assert "user_ids=kristen" in message
    assert "user_ids=henrique" not in message
    assert "email=sent recipient_count=1" in message
    assert device.calls == ["on"]
