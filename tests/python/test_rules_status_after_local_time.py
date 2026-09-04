"""Unit tests for rules_status.py's after_local_time eligibility-wake branch."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterLocalTimeCondition,
    Ep1ReadingCompareCondition,
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
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_evaluator import RuleEvaluator
from app.rules_status import build_rules_status
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_MAC = "aa:bb:cc:dd:ee:03"
_RULE_ID = "evening-lux-after-local-time-status"


def test_rules_status_reports_next_evaluate_at_for_after_local_time_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    tz = ZoneInfo("America/New_York")
    gate_open = datetime(2023, 11, 14, 21, 0, tzinfo=tz)
    now = gate_open - timedelta(hours=2)
    _seed_presence_db(db, now=now.timestamp())
    device = _FakeEp1(_MAC, "Office EP1", illuminance_lx=20.0)
    state = DeviceManagersState(
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
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
        now_fn=lambda: now.timestamp(),
    )

    status = build_rules_status(cache_path=db, device_state=state, evaluator=evaluator, now=now)
    row = next(r for r in status.rules if r.id == _RULE_ID)

    assert row.next_evaluate_at is not None
    assert row.scheduled_detail is not None
    assert row.scheduled_detail.startswith("Evaluates once when eligible at 9:00 PM")
    assert "watched device/reading changes" in row.scheduled_detail


def _ep1_mgr(device: _FakeEp1) -> Ep1DeviceManager:
    mgr = Ep1DeviceManager.__new__(Ep1DeviceManager)
    mgr._devices = {device.identifier: device}  # type: ignore[attr-defined]
    mgr._fetched = True  # type: ignore[attr-defined]
    return mgr


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


def _rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
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
        id=_RULE_ID,
        label="Evening lux after 9pm (status)",
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
