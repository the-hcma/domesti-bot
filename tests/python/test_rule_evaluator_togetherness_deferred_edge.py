"""Hermetic tests for deferring a geofence edge when ``users_together`` is unknown.

Yesterday's field case (2026-10-19 15:43): Henrique arrived home with a good
fix while Kristen's newest usable fix was almost two minutes old, so the
"not travelling together" condition was unknown and the dry-run garage rule
was skipped with no retry. The edge must now be deferred for the rule's
accuracy-edge grace, the stale peer must be asked for a fresh location, and a
later report from either user must re-attempt the fire.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
    UsersTogetherCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_T0 = 1_700_000_000.0
_HOME_LAT = 41.194072
_HOME_LON = -73.8883254
_M_PER_DEG_LAT = 2 * math.pi * 6_371_000.0 / 360.0


class _FakeKasa:
    def __init__(self, host: str, label: str) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.host = host
        self.mac_address = None
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")


def _kasa_mgr(device: _FakeKasa) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = (device,)
    mgr.get_device_by_alias.return_value = None
    return cast(KasaDeviceManager, mgr)


def _rule(*, extra_conditions: tuple = ()) -> RuleOut:
    return RuleOut(
        accuracy_edge_grace_s=120,
        conditions=RuleConditionsOut(
            all=[
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
                UsersTogetherCondition(
                    type="users_together",
                    expected=False,
                    far_m=150.0,
                    min_confidence=0.8,
                    near_m=30.0,
                    user_ids=["henrique", "kristen"],
                ),
                *extra_conditions,
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
        id="henrique-enters-alone",
        label="Henrique enters alone",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


class _Rig:
    """Two users, one geofence and the alone-entry rule, on a simulated clock."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rule: RuleOut | None = None) -> None:
        bundle = tmp_path / "rules.json"
        payload = {
            "version": 1,
            "device_id_resolution": "preferred_label",
            "settings_location": {
                "lat": _HOME_LAT,
                "lon": _HOME_LON,
                "timezone": "America/New_York",
                "home_label": "Home",
            },
            "rules": [(rule or _rule()).model_dump(mode="json")],
        }
        bundle.write_text(json.dumps(payload), encoding="utf-8")
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
        self.db = tmp_path / "discovery.sqlite"
        self.clock = {"now": _T0}
        replace_users(
            self.db,
            [
                UserRecord(
                    user_id=user_id,
                    first_name=user_id,
                    last_name="",
                    display_name=user_id,
                    tracking_device_label="Phone",
                    enabled=True,
                )
                for user_id in ("henrique", "kristen")
            ],
        )
        replace_geofences(
            self.db,
            [
                GeofenceRecord(
                    geofence_id="house",
                    label="House",
                    center_lat=_HOME_LAT,
                    center_lon=_HOME_LON,
                    radius_m=250,
                    enabled=True,
                    owntracks_rid=None,
                ),
            ],
        )
        self.device = _FakeKasa("192.168.1.10", "Garage")
        device = self.device
        db = self.db
        self.evaluator = RuleEvaluator(
            cache_path=self.db,
            device_state_getter=lambda: DeviceManagersState(
                kasa_mgr=_kasa_mgr(device),
                sonos_mgr=None,
                tailwind_mgr=None,
                androidtv_mgr=None,
                ep1_mgr=None,
                vizio_mgr=None,
                cache_path=db,
                args=argparse.Namespace(),
            ),
            now_fn=lambda: self.clock["now"],
        )
        self.coordinator = MagicMock()
        self.evaluator._location_request_coordinator = self.coordinator

    async def report(self, user_id: str, at_s: float, *, north_m: float) -> None:
        at = _T0 + at_s
        self.clock["now"] = at
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id=user_id,
                lat=_HOME_LAT + north_m / _M_PER_DEG_LAT,
                lon=_HOME_LON,
                accuracy_m=5,
                fix_at=at,
                reported_at=at,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await self.evaluator.on_location_update(user_id)


async def _drive_to_stale_peer_arrival(rig: _Rig) -> None:
    """Henrique walks home from 800 m north; Kristen stays 800 m south but goes quiet.

    The walk takes 360 s so the geofence enter is not dwell-suppressed (a user
    must have been outside for at least 300 s for an enter to count).
    """
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-800.0)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 310.0, north_m=-810.0)
    await rig.report("henrique", 360.0, north_m=10.0)


@pytest.mark.asyncio
async def test_unknown_togetherness_defers_edge_and_requests_peer_location(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await _drive_to_stale_peer_arrival(rig)

    # Kristen's newest usable fix is 50 s old (> 45 s allowed): unknown, not apart.
    assert rig.device.calls == []
    snapshots = rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique")
    assert len(snapshots) == 1
    assert snapshots[0].rule_id == "henrique-enters-alone"
    requests = rig.coordinator.schedule_request_with_reason.call_args_list
    assert len(requests) == 1
    assert requests[0].args[0] == "kristen"
    assert requests[0].kwargs["reason"] == "togetherness_unknown"
    assert requests[0].kwargs["rule_id"] == "henrique-enters-alone"


@pytest.mark.asyncio
async def test_no_deferral_when_another_condition_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api.schemas import AfterLocalTimeCondition

    # 21:00 local has not happened yet at the simulated arrival (17:13 EST), so
    # a fresh peer fix could never make this rule fire: do not defer the edge
    # and do not wake Kristen's phone.
    rule = _rule(extra_conditions=(AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),))
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    await _drive_to_stale_peer_arrival(rig)

    assert rig.device.calls == []
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []


@pytest.mark.asyncio
async def test_deferred_edge_fires_when_peer_reports_fresh_fixes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await _drive_to_stale_peer_arrival(rig)
    assert rig.device.calls == []

    # Kristen's phone wakes and reports every ~20 s, still far south; Henrique
    # keeps reporting from home. Once their tracks overlap enough to compare,
    # the pair is known-apart and the deferred edge fires.
    await rig.report("kristen", 380.0, north_m=-800.0)
    await rig.report("henrique", 395.0, north_m=10.0)
    await rig.report("kristen", 400.0, north_m=-790.0)
    await rig.report("henrique", 415.0, north_m=10.0)
    assert rig.device.calls == []
    await rig.report("kristen", 420.0, north_m=-780.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()


@pytest.mark.asyncio
async def test_deferred_edge_expires_when_peer_stays_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await _drive_to_stale_peer_arrival(rig)
    assert rig.device.calls == []

    # Well past the 120 s grace, fresh fixes must not resurrect the old edge:
    # Henrique never left and re-entered, so there is no new arrival to act on.
    await rig.report("kristen", 660.0, north_m=-800.0)
    await rig.report("henrique", 670.0, north_m=10.0)
    await rig.report("kristen", 680.0, north_m=-790.0)

    assert rig.device.calls == []
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()


@pytest.mark.asyncio
async def test_known_apart_arrival_fires_immediately_without_deferring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-800.0)
    await rig.report("henrique", 100.0, north_m=650.0)
    await rig.report("henrique", 180.0, north_m=520.0)
    await rig.report("henrique", 240.0, north_m=400.0)
    await rig.report("kristen", 260.0, north_m=-800.0)
    await rig.report("henrique", 280.0, north_m=280.0)
    await rig.report("kristen", 285.0, north_m=-800.0)
    await rig.report("kristen", 305.0, north_m=-800.0)
    await rig.report("kristen", 318.0, north_m=-800.0)
    await rig.report("henrique", 320.0, north_m=150.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []
