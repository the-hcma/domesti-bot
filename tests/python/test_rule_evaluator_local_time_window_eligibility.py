"""Hermetic tests for local_time_window eligibility wake-ups (#672)."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import TypedDict
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    Ep1ReadingCompareCondition,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import (
    DeviceFamilyId,
    Ep1ReadingComparison,
    Ep1ReadingMetric,
    RuleTrigger,
)
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


@pytest.mark.asyncio
async def test_local_time_window_eligibility_fires_once_at_window_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _window_fixture(tmp_path, monkeypatch)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    window_start = fixture["window_start"]

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        clock["now"] = (window_start - timedelta(minutes=5)).timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 0

        clock["now"] = window_start.timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1

        clock["now"] = window_start.timestamp() + 120.0
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_local_time_window_eligibility_materializes_next_evaluate_at_on_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _window_fixture(tmp_path, monkeypatch)
    evaluator = fixture["evaluator"]
    window_start = fixture["window_start"]

    next_at = evaluator.next_evaluate_at_for_rule("evening-lux-window")
    assert next_at == pytest.approx(window_start.timestamp())
    cron = evaluator.effective_schedule_cron_for_rule("evening-lux-window")
    assert cron == "0 21 * * *"


@pytest.mark.asyncio
async def test_after_window_end_reading_wake_conditions_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _window_fixture(tmp_path, monkeypatch, start_inside_window=False)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    window_start = fixture["window_start"]

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        # After midnight — window closed; lux wake still runs but conditions fail.
        clock["now"] = (window_start + timedelta(hours=4)).timestamp()
        await evaluator.on_device_state_change(
            DeviceFamilyId.EP1,
            _MAC,
            reading_metric=Ep1ReadingMetric.ILLUMINANCE_LX,
        )
        assert send_mock.call_count == 0


_MAC = "aa:bb:cc:dd:ee:01"


class _WindowFixture(TypedDict):
    clock: dict[str, float]
    evaluator: RuleEvaluator
    window_start: datetime


class _FakeEp1:
    def __init__(self, identifier: str, label: str, *, illuminance_lx: float) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self.host = "192.0.2.10"
        self.port = 6053
        self.illuminance_lx = illuminance_lx
        self.humidity_pct = None
        self.temperature_c = None
        self.occupancy_state = None
        self.unresponsive = False


def _ep1_mgr(device: _FakeEp1) -> Ep1DeviceManager:
    mgr = Ep1DeviceManager.__new__(Ep1DeviceManager)
    mgr._devices = {device.identifier: device}  # type: ignore[attr-defined]
    mgr._fetched = True  # type: ignore[attr-defined]
    return mgr


def _evening_lux_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                LocalTimeWindowCondition(
                    type="local_time_window",
                    start_hhmm="21:00",
                    end_hhmm="00:00",
                ),
                Ep1ReadingCompareCondition(
                    type="ep1_reading_compare",
                    comparison=Ep1ReadingComparison.BELOW,
                    metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                    threshold=34.0,
                    device=RuleConditionDeviceRefOut(
                        device_id=_MAC,
                        family_id=DeviceFamilyId.EP1,
                        display_name="Office EP1",
                    ),
                ),
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        fire_once_per_local_day=True,
        id="evening-lux-window",
        label="Evening lux window",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
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
            fix_at=now,
            reported_at=now,
            source="test",
        ),
        retention=default_location_history_retention(),
    )


def _window_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_inside_window: bool = False,
) -> _WindowFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_lux_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    tz = ZoneInfo("America/New_York")
    window_start = datetime(2023, 11, 14, 21, 0, tzinfo=tz)
    if start_inside_window:
        clock = {"now": (window_start + timedelta(minutes=30)).timestamp()}
    else:
        clock = {"now": (window_start - timedelta(hours=2)).timestamp()}

    _seed_presence_db(db, now=clock["now"])
    device = _FakeEp1(_MAC, "Office EP1", illuminance_lx=20.0)
    state = DeviceManagersState(
        kasa_mgr=None,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=_ep1_mgr(device),
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
        "window_start": window_start,
    }


def _write_bundle(path: Path, rule: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "mac",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json")],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
