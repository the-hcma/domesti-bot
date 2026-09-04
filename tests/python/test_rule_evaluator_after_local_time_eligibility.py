"""Hermetic tests for after_local_time eligibility wake-ups (#711 follow-up)."""

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
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_MAC = "aa:bb:cc:dd:ee:02"


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


class _GateFixture(TypedDict):
    clock: dict[str, float]
    evaluator: RuleEvaluator
    gate_open: datetime


@pytest.mark.asyncio
async def test_after_local_time_eligibility_fires_once_at_gate_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _gate_fixture(tmp_path, monkeypatch)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]
    gate_open = fixture["gate_open"]

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        clock["now"] = (gate_open - timedelta(minutes=5)).timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 0

        clock["now"] = gate_open.timestamp()
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1

        clock["now"] = gate_open.timestamp() + 120.0
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_after_local_time_eligibility_materializes_next_evaluate_at_on_boot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _gate_fixture(tmp_path, monkeypatch)
    evaluator = fixture["evaluator"]
    gate_open = fixture["gate_open"]

    next_at = evaluator.next_evaluate_at_for_rule("evening-lux-after-local-time")
    assert next_at == pytest.approx(gate_open.timestamp())
    cron = evaluator.effective_schedule_cron_for_rule("evening-lux-after-local-time")
    assert cron == "0 21 * * *"


@pytest.mark.asyncio
async def test_after_local_time_eligibility_prompt_when_boot_after_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _gate_fixture(tmp_path, monkeypatch, start_after_gate=True)
    clock = fixture["clock"]
    evaluator = fixture["evaluator"]

    next_at = evaluator.next_evaluate_at_for_rule("evening-lux-after-local-time")
    assert next_at == pytest.approx(clock["now"])

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1


@pytest.mark.asyncio
async def test_after_local_time_forced_new_day_refresh_does_not_reprompt_same_day(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced new-day materialization must not re-fire eligibility mid-day."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_lux_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    tz = ZoneInfo("America/New_York")
    mid_day = datetime(2023, 11, 15, 12, 0, tzinfo=tz)
    clock = {"now": mid_day.timestamp()}
    _seed_presence_db(db, now=clock["now"])
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
        now_fn=lambda: clock["now"],
    )
    from app.automation_rules_loader import list_automation_rules
    from app.rule_evaluator import _RuleRuntimeState

    rule = next(r for r in list_automation_rules() if r.id == "evening-lux-after-local-time")
    evaluator._rule_state["evening-lux-after-local-time"] = _RuleRuntimeState()
    cron = evaluator._ensure_after_local_time_schedule_materialized(
        rule,
        timezone=tz,
        now=mid_day,
        force=True,
    )
    assert cron == "0 21 * * *"
    next_at = evaluator.next_evaluate_at_for_rule("evening-lux-after-local-time")
    expected = datetime(2023, 11, 15, 21, 0, tzinfo=tz).timestamp()
    assert next_at == pytest.approx(expected)
    assert next_at != pytest.approx(clock["now"])


@pytest.mark.asyncio
async def test_after_local_time_schedule_advance_persists_across_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A same-day restart after the rule has fired must not re-fire it — the
    advanced (tomorrow's) next_evaluate_at has to be persisted, not just held
    in memory, the same way the astronomical / local_time_window paths do."""
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    # fire_once_per_local_day=False / cooldown_s=0 so a stale persisted
    # next_evaluate_at is the only thing that could suppress a re-fire.
    rule = _evening_lux_rule().model_copy(update={"cooldown_s": 0, "fire_once_per_local_day": False})
    _write_bundle(bundle, rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    tz = ZoneInfo("America/New_York")
    gate_open = datetime(2023, 11, 14, 21, 0, tzinfo=tz)
    clock = {"now": (gate_open - timedelta(hours=2)).timestamp()}
    _seed_presence_db(db, now=clock["now"])
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
        now_fn=lambda: clock["now"],
    )

    clock["now"] = gate_open.timestamp()
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator._evaluate_scheduled_rules()
        assert send_mock.call_count == 1

    # Simulate a same-day restart (deploy/crash): a fresh evaluator reloads
    # whatever schedule state was persisted.
    restarted = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: clock["now"],
    )
    next_at = restarted.next_evaluate_at_for_rule("evening-lux-after-local-time")
    assert next_at is not None
    assert next_at > clock["now"]

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await restarted._evaluate_scheduled_rules()
        assert send_mock.call_count == 0


def _ep1_mgr(device: _FakeEp1) -> Ep1DeviceManager:
    mgr = Ep1DeviceManager.__new__(Ep1DeviceManager)
    mgr._devices = {device.identifier: device}  # type: ignore[attr-defined]
    mgr._fetched = True  # type: ignore[attr-defined]
    return mgr


def _evening_lux_rule() -> RuleOut:
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
        fire_once_per_local_day=True,
        id="evening-lux-after-local-time",
        label="Evening lux after 9pm",
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


def _gate_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    start_after_gate: bool = False,
) -> _GateFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _evening_lux_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    tz = ZoneInfo("America/New_York")
    gate_open = datetime(2023, 11, 14, 21, 0, tzinfo=tz)
    if start_after_gate:
        clock = {"now": (gate_open + timedelta(minutes=30)).timestamp()}
    else:
        clock = {"now": (gate_open - timedelta(hours=2)).timestamp()}

    _seed_presence_db(db, now=clock["now"])
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
        now_fn=lambda: clock["now"],
    )
    return {
        "clock": clock,
        "evaluator": evaluator,
        "gate_open": gate_open,
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
