"""Hermetic tests for astronomical eligibility wake-ups (issue #393)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    DevicesAnyInStateCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    RulesSunOut,
    UsersInsideGeofenceForSCondition,
)
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


@pytest.mark.asyncio
async def test_device_state_fires_after_sunset_when_light_turns_on_with_dwell_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _eligibility_fixture(
        tmp_path,
        monkeypatch,
        lights_on=False,
        start_at_sunset=True,
    )
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    front = fixture["front"]

    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 700.0
    await evaluator.on_location_update("henrique")
    assert front.calls == []

    front.is_on = True
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_device_state_change(
            DeviceFamilyId.KASA,
            "Front door lights",
        )
        assert send_mock.call_count == 1
        assert front.calls == ["off"]


@pytest.mark.asyncio
async def test_dwell_satisfied_fires_after_sunset_when_inside_dwell_elapses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _eligibility_fixture(
        tmp_path,
        monkeypatch,
        start_at_sunset=True,
    )
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]

    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 700.0
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_location_update("kristen")
        assert send_mock.call_count == 1
        assert fixture["front"].calls == ["off"]


@pytest.mark.asyncio
async def test_eligibility_wake_daytime_dwell_does_not_poison_evening_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _eligibility_fixture(tmp_path, monkeypatch)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    sunset_local = fixture["sunset_local"]

    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 700.0
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_location_update("henrique")
        assert send_mock.call_count == 0

        clock["now"] = sunset_local.timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_eligibility_wake_fires_at_sunset_when_dwell_and_devices_already_met(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Common path: home + lights on before sunset; fire once when eligible."""
    fixture = _eligibility_fixture(tmp_path, monkeypatch)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    front = fixture["front"]
    garage = fixture["garage"]
    sunset_local = fixture["sunset_local"]

    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 700.0
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_location_update("henrique")
        assert send_mock.call_count == 0
        assert front.calls == []
        assert garage.calls == []

        clock["now"] = sunset_local.timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1
        assert front.calls == ["off"]
        assert garage.calls == ["off"]

        clock["now"] = sunset_local.timestamp() + 120.0
        await evaluator._evaluate_scheduled_rules()
        await evaluator.on_location_update("kristen")
        assert send_mock.call_count == 1
        assert front.calls == ["off"]
        assert garage.calls == ["off"]


@pytest.mark.asyncio
async def test_eligibility_wake_materializes_next_evaluate_at_on_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _eligibility_fixture(tmp_path, monkeypatch)
    evaluator = fixture["evaluator"]
    sunset_local = fixture["sunset_local"]

    next_at = evaluator.next_evaluate_at_for_rule("evening-lights-off-both-home")
    assert next_at == pytest.approx(sunset_local.timestamp())
    cron = evaluator.effective_schedule_cron_for_rule("evening-lights-off-both-home")
    assert cron is not None
    assert cron.count(" ") == 4


class _EligibilityFixture(TypedDict):
    clock: dict[str, float]
    evaluator: RuleEvaluator
    front: _FakeKasa
    garage: _FakeKasa
    sunset_local: datetime


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.host = host
        self.mac_address = None
        self.identifier = host
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.is_on = False
        self.calls.append("off")


def _eligibility_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    lights_on: bool = True,
    start_at_sunset: bool = False,
) -> _EligibilityFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_lights_off_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    monkeypatch.setattr(
        "app.rule_evaluator.compute_rules_sun_out",
        lambda *args, **kwargs: _mock_sun_for_nov_14_2023(),
    )

    tz = ZoneInfo("America/New_York")
    sunset_local = datetime.fromisoformat("2023-11-14T22:30:00Z").astimezone(tz)
    if start_at_sunset:
        clock = {"now": sunset_local.timestamp() + 60.0}
    else:
        clock = {"now": (sunset_local - timedelta(hours=2)).timestamp()}

    _seed_presence_db(db, now=clock["now"])
    front = _FakeKasa("192.168.1.10", "Front door lights", is_on=lights_on)
    garage = _FakeKasa("192.168.1.11", "Garage outside lights", is_on=lights_on)
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([front, garage]),
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
        "clock": clock,
        "evaluator": evaluator,
        "front": front,
        "garage": garage,
        "sunset_local": sunset_local,
    }


def _evening_lights_off_rule() -> RuleOut:
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
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Front door lights",
                action=RuleDeviceActionType.TURN_OFF,
            ),
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Garage outside lights",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
        enabled=True,
        id="evening-lights-off-both-home",
        label="Evening lights off both home",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE, RuleTrigger.DWELL_SATISFIED],
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
                lat=41.194085,
                lon=-73.888365,
                accuracy_m=20,
                fix_at=now,
                reported_at=now,
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
