"""Hermetic tests for device-dwell rule triggers (garage open while away)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    DevicesAnyInStateForSCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    UsersOutsideGeofenceForSCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


@pytest.mark.asyncio

async def test_device_dwell_does_not_fire_when_door_just_opened_while_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    door.is_open = True
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    send_mock.assert_not_called()
    assert evaluator.fire_state_for_rule("away-garage-open-alert").last_fired_at is None


@pytest.mark.asyncio

async def test_device_dwell_fires_once_per_away_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0
    door.is_open = True

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        assert send_mock.call_count == 1

        door.is_open = False
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        door.is_open = True
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    assert send_mock.call_count == 1

async def test_device_dwell_fires_when_door_open_for_threshold_while_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_garage_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    door = _FakeTailwindDoor("door-left", "Left", is_open=False)
    state = _tailwind_state(door)
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    door.is_open = True
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )
        assert send_mock.call_count == 0
        clock["now"] += 1200.0
        await evaluator._maybe_process_device_dwell_satisfied(
            DeviceFamilyId.TAILWIND,
            "door-left",
        )

    send_mock.assert_called_once()
    assert evaluator.fire_state_for_rule("away-garage-open-alert").last_fired_at == (
        clock["now"]
    )


@pytest.mark.asyncio

class _FakeTailwindDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_open = is_open


def _away_garage_rule(*, cooldown_s: int = 0) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique", "kristen"],
                ),
                DevicesAnyInStateForSCondition(
                    type="devices_any_in_state_for_s",
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id="Left",
                            family_id=DeviceFamilyId.TAILWIND,
                        ),
                    ],
                    min_duration_s=1200,
                    state=DeviceConditionState.OPEN,
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[],
        enabled=True,
        id="away-garage-open-alert",
        label="Away garage open alert",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DWELL_SATISFIED],
    )


def _seed_presence_db(db: Path, *, now: float) -> None:
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
    for user_id in ("henrique", "kristen"):
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id=user_id,
                lat=44.0,
                lon=-73.0,
                accuracy_m=20,
                fix_at=now,
                reported_at=now,
                source="test",
            ),
            retention=default_location_history_retention(),
        )


def _tailwind_state(*doors: _FakeTailwindDoor) -> DeviceManagersState:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=None,
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=mgr,
        vizio_mgr=None,
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
