"""Hermetic tests for the asyncio rule evaluator."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleActionDispatchError
from app.rule_evaluator import (
    GeofenceTransition,
    RuleEvaluator,
    _user_triggered_geofence_edge,
)
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


@dataclass
class _ArriveHomeFixture:
    clock: dict[str, float]
    db: Path
    device: _FakeKasa
    evaluator: RuleEvaluator


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


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
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


def _henrique_inside_house_condition() -> UsersInsideGeofenceCondition:
    return UsersInsideGeofenceCondition(
        type="users_inside_geofence",
        geofence_id="house",
        user_ids=["henrique"],
    )


def _arrive_home_rule(*, cooldown_s: int) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[_henrique_inside_house_condition()]),
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


def _setup_arrive_home_evaluator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cooldown_s: int,
    device_state_getter: Callable[[], DeviceManagersState | None] | None = None,
) -> _ArriveHomeFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _arrive_home_rule(cooldown_s=cooldown_s))
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
    state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([device]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=device_state_getter or (lambda: state),
        now_fn=lambda: clock["now"],
    )
    return _ArriveHomeFixture(
        clock=clock,
        db=db,
        device=device,
        evaluator=evaluator,
    )


def _move_henrique_inside_house(fixture: _ArriveHomeFixture) -> None:
    fixture.clock["now"] += 60.0
    upsert_user_location(
        fixture.db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.194085,
            lon=-73.888365,
            accuracy_m=20,
            received_at=fixture.clock["now"],
            source="test",
        ),
        retention=default_location_history_retention(),
    )


@pytest.mark.asyncio
async def test_rule_evaluator_does_not_refire_while_staying_inside(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=0,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]


@pytest.mark.asyncio
async def test_rule_evaluator_does_not_stamp_fired_when_device_discovery_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=300,
        device_state_getter=lambda: None,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    fire_state = fixture.evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at is None
    assert fire_state.last_error is not None
    assert "discovery still in progress" in fire_state.last_error.lower()


@pytest.mark.asyncio
async def test_rule_evaluator_records_error_when_notification_email_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    notify_rule = _arrive_home_rule(cooldown_s=300).model_copy(
        update={
            "device_actions": [],
            "notification_email": "ops@example.com",
            "notify_on_fire": True,
        },
    )
    _write_bundle(bundle, notify_rule)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(
        db,
        user_id="henrique",
        lat=44.0,
        lon=-73.0,
        received_at=clock["now"],
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
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
    with (
        patch(
            "app.rule_evaluator.send_rule_notification_email",
            side_effect=RuleActionDispatchError("SMTP is not configured"),
        ),
        patch("app.rule_evaluator._LOGGER.warning") as warning_mock,
    ):
        await evaluator.on_location_update("henrique")

    fire_state = evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at is None
    assert fire_state.last_error == "SMTP is not configured"
    warning_mock.assert_called_once_with(
        "[rules] rule_id=%s edge matched but no side effect completed: %s",
        "arrive-home",
        "SMTP is not configured",
    )


@pytest.mark.asyncio
async def test_rule_evaluator_fires_on_geofence_enter_edge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _setup_arrive_home_evaluator(
        tmp_path,
        monkeypatch,
        cooldown_s=300,
    )
    _move_henrique_inside_house(fixture)
    await fixture.evaluator.on_location_update("henrique")
    assert fixture.device.calls == ["on"]
    fire_state = fixture.evaluator.fire_state_for_rule("arrive-home")
    assert fire_state.last_fired_at == fixture.clock["now"]


def test_user_triggered_geofence_edge_requires_enter_for_inside_rule() -> None:
    conditions = RuleConditionsOut(all=[_henrique_inside_house_condition()]).all
    assert _user_triggered_geofence_edge(
        conditions,
        "henrique",
        {"house": GeofenceTransition(entered=True)},
    )
    assert not _user_triggered_geofence_edge(
        conditions,
        "henrique",
        {"house": GeofenceTransition(left=True)},
    )
