"""Hermetic tests for automation rule evaluator diagnostic logging."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
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


def _seed_presence_db(
    cache_path: Path,
    *,
    user_id: str,
    lat: float,
    lon: float,
    received_at: float,
) -> None:
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
            received_at=received_at,
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
        notification_email=None,
        notify_on_fire=False,
        trigger="edge_true",
    )


def _rules_log_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if "[rules]" in record.getMessage()]


@pytest.mark.asyncio
async def test_fired_log_includes_user_transitions_and_conditions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        received_at=clock["now"],
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
            received_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await evaluator.on_location_update("henrique")

    fired = [
        record
        for record in _rules_log_records(caplog)
        if "fired rule_id=arrive-home" in record.getMessage()
    ]
    assert fired
    message = fired[0].getMessage()
    assert "user_id=henrique" in message
    assert "house:entered" in message
    assert "conditions=" in message


@pytest.mark.asyncio
async def test_debounced_geofence_enter_logs_suppressed_at_info(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        received_at=clock["now"],
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
            received_at=clock["now"],
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
            received_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await evaluator.on_location_update("henrique")

    suppressed = [
        record
        for record in _rules_log_records(caplog)
        if "geofence enter suppressed" in record.getMessage()
    ]
    assert suppressed
    assert "user_id=henrique" in suppressed[0].getMessage()
    assert "geofence_id=house" in suppressed[0].getMessage()


@pytest.mark.asyncio
async def test_conditions_not_met_logs_skip_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        received_at=clock["now"],
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
            received_at=clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )

    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await evaluator.on_location_update("henrique")

    skipped = [
        record
        for record in _rules_log_records(caplog)
        if "skipped rule_id=arrive-home" in record.getMessage()
        and "reason=conditions_not_met" in record.getMessage()
    ]
    assert skipped
    assert device.calls == []
