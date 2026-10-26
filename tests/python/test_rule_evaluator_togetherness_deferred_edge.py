"""Hermetic tests for deferring a geofence edge when ``users_together`` is unknown.

Field cases this covers:

- 2026-10-19: Henrique arrived with a good fix while Kristen's newest usable fix
  was almost two minutes old (stale peer), so the alone garage entry rule was
  skipped with no retry.
- 2026-10-25 (#916): they arrived together, but Kristen's phone briefly reported
  only coarse network fixes the estimator drops, leaving too-little last-minute
  coverage at Henrique's enter; together and alone garage entry rules both
  skipped and never retried.

The edge must be deferred for the rule's accuracy-edge grace, peers must be
asked for denser fixes, and a later report from either user must re-attempt
the fire.
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
    AfterLocalTimeCondition,
    AnyConditionsCondition,
    RuleConditionOut,
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
from app.rule_evaluator import RuleEvaluator, _DeferredAccuracyEdge
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


def _rule(
    *,
    cooldown_s: int = 0,
    expected_together: bool = False,
    extra_conditions: tuple[RuleConditionOut, ...] = (),
    rule_id: str | None = None,
    rule_label: str | None = None,
) -> RuleOut:
    alone = not expected_together
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
                    expected=expected_together,
                    far_m=150.0,
                    min_confidence=0.8,
                    near_m=30.0,
                    user_ids=["henrique", "kristen"],
                ),
                *extra_conditions,
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
        id=rule_id or ("henrique-enters-alone" if alone else "henrique-enters-together"),
        label=rule_label or ("Henrique enters alone" if alone else "Henrique enters together"),
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


class _Rig:
    """Two users, one geofence and the alone-entry rule, on a simulated clock."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        rule: RuleOut | None = None,
        user_ids: tuple[str, ...] = ("henrique", "kristen"),
    ) -> None:
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
                for user_id in user_ids
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

    async def report(
        self,
        user_id: str,
        at_s: float,
        *,
        north_m: float,
        accuracy_m: int = 5,
        fix_at_s: float | None = None,
    ) -> None:
        at = _T0 + at_s
        self.clock["now"] = at
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id=user_id,
                lat=_HOME_LAT + north_m / _M_PER_DEG_LAT,
                lon=_HOME_LON,
                accuracy_m=accuracy_m,
                fix_at=at if fix_at_s is None else _T0 + fix_at_s,
                reported_at=at,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await self.evaluator.on_location_update(user_id)


async def _drive_to_stale_peer_arrival(rig: _Rig) -> None:
    """Henrique walks home from 800 m north with Kristen right behind him, but her phone goes quiet.

    Kristen was beside him within the last five minutes, so her stale fix leaves the pair unknown instead of
    apart. The walk takes 360 s so the geofence enter is not dwell-suppressed (a user must have been outside
    for at least 300 s for an enter to count).
    """
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=790.0)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 310.0, north_m=130.0)
    await rig.report("henrique", 360.0, north_m=10.0)


async def _drive_to_sparse_coverage_joint_arrival(rig: _Rig) -> None:
    """Both phones travel home together; Kristen's usable track briefly thins out before Henrique enters.

    Mirrors 2026-10-25 (#916): dense co-travel, then Kristen goes quiet for longer than
    ``max_gap_s`` while Henrique keeps reporting, then she lands one fresh fix a few seconds
    before his enter. Both newest usable fixes are still fresh, but the last-minute window
    has too few paired instants (the gap cannot be interpolated).
    """
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=800.0)
    # Stay outside the 250 m house geofence until the final enter.
    for at_s, north_m in (
        (120.0, 650.0),
        (240.0, 500.0),
        (300.0, 400.0),
        (320.0, 350.0),
    ):
        await rig.report("henrique", at_s, north_m=north_m)
        await rig.report("kristen", at_s, north_m=north_m)
    # > max_gap_s (45 s) with only Henrique reporting; Kristen stays silent.
    await rig.report("henrique", 330.0, north_m=330.0)
    await rig.report("henrique", 340.0, north_m=310.0)
    await rig.report("henrique", 350.0, north_m=290.0)
    await rig.report("henrique", 360.0, north_m=280.0)
    await rig.report("henrique", 370.0, north_m=270.0)
    # One fresh Kristen fix (~5 s before enter) keeps the pair out of STALE_FIX.
    await rig.report("kristen", 375.0, north_m=260.0)
    await rig.report("henrique", 380.0, north_m=10.0)


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
async def test_too_little_coverage_defers_together_edge_and_requests_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Joint arrival with a brief peer reporting gap must defer, not drop, the together entry (#916)."""
    rule = _rule(expected_together=True)
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    await _drive_to_sparse_coverage_joint_arrival(rig)

    assert rig.device.calls == []
    snapshots = rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique")
    assert len(snapshots) == 1
    assert snapshots[0].rule_id == "henrique-enters-together"
    requests = rig.coordinator.schedule_request_with_reason.call_args_list
    assert len(requests) == 1
    assert requests[0].args[0] == "kristen"
    assert requests[0].kwargs["reason"] == "togetherness_unknown"
    assert requests[0].kwargs["rule_id"] == "henrique-enters-together"


@pytest.mark.asyncio
async def test_too_little_coverage_deferred_together_edge_fires_when_peer_track_fills_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a sparse-coverage deferral, denser peer fixes beside the arriver must fire together (#916)."""
    rule = _rule(expected_together=True)
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    await _drive_to_sparse_coverage_joint_arrival(rig)
    assert rig.device.calls == []
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") != ()

    # Kristen resumes next to Henrique inside the fence; overlapping reports rebuild coverage.
    await rig.report("kristen", 390.0, north_m=12.0)
    await rig.report("henrique", 400.0, north_m=10.0)
    await rig.report("kristen", 410.0, north_m=12.0)
    await rig.report("henrique", 420.0, north_m=10.0)
    await rig.report("kristen", 430.0, north_m=12.0)
    await rig.report("henrique", 440.0, north_m=10.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()


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
async def test_no_deferral_when_another_together_pair_is_known_unmet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One stale peer must not defer an arrival another pair already settles.

    Henrique arrives with Sam (a second ``users_together`` pair, expected
    apart, is known-unmet) while Kristen's fix is stale (the first pair is
    unknown). The arrival is already known not to qualify, so no edge is
    deferred and Kristen's phone is not woken.
    """
    base_rule = _rule()
    inside_condition, together_condition = base_rule.conditions.all
    together_with_sam = together_condition.model_copy(update={"user_ids": ["henrique", "sam"]})
    rule = base_rule.model_copy(
        update={"conditions": RuleConditionsOut(all=[inside_condition, together_condition, together_with_sam])},
    )
    rig = _Rig(tmp_path, monkeypatch, rule=rule, user_ids=("henrique", "kristen", "sam"))
    await rig.report("kristen", 0.0, north_m=-800.0)
    await rig.report("sam", 0.0, north_m=800.0)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 120.0, north_m=-800.0)
    await rig.report("sam", 120.0, north_m=650.0)
    await rig.report("henrique", 120.0, north_m=650.0)
    await rig.report("kristen", 240.0, north_m=-800.0)
    await rig.report("sam", 240.0, north_m=450.0)
    await rig.report("henrique", 240.0, north_m=450.0)
    await rig.report("kristen", 310.0, north_m=-810.0)
    await rig.report("sam", 330.0, north_m=300.0)
    await rig.report("henrique", 330.0, north_m=300.0)
    await rig.report("sam", 360.0, north_m=15.0)
    await rig.report("henrique", 360.0, north_m=10.0)
    await rig.report("sam", 380.0, north_m=15.0)
    await rig.report("henrique", 380.0, north_m=10.0)
    await rig.report("sam", 400.0, north_m=15.0)
    await rig.report("henrique", 400.0, north_m=10.0)

    assert rig.device.calls == []
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []


@pytest.mark.asyncio
async def test_deferred_retry_does_not_request_peer_when_another_condition_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deferred retry must not wake the peer while a time gate also blocks the rule."""
    from app.api.schemas import AfterLocalTimeCondition

    rule = _rule(extra_conditions=(AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),))
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    await _drive_to_stale_peer_arrival(rig)
    # The closed time gate already suppressed deferral at arrival; stage an
    # accuracy-deferred edge directly to exercise the retry path.
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    arrived_at = _T0 + 360.0
    rig.evaluator._deferred_accuracy_edges[("henrique-enters-alone", "henrique", "house", "entered")] = (
        _DeferredAccuracyEdge(
            event="entered",
            expires_at=arrived_at + 120.0,
            geofence_id="house",
            observed_at=arrived_at,
            reason="location_accuracy",
            rule_id="henrique-enters-alone",
            user_id="henrique",
        )
    )
    rig.coordinator.schedule_request_with_reason.reset_mock()

    await rig.report("henrique", 380.0, north_m=10.0)

    # The edge stays pending, but no fresh peer fix could open the time gate,
    # so Kristen's phone is left alone.
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []
    assert len(rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique")) == 1
    assert rig.device.calls == []


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
async def test_deferred_edge_cancelled_when_pair_arrives_together_then_separates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await _drive_to_stale_peer_arrival(rig)
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") != ()

    # Kristen's phone wakes next to Henrique at home: the pair is known
    # together, so the alone-entry arrival is settled and the edge is dropped.
    await rig.report("kristen", 380.0, north_m=15.0)
    await rig.report("henrique", 385.0, north_m=10.0)
    await rig.report("kristen", 390.0, north_m=15.0)
    await rig.report("kristen", 400.0, north_m=15.0)
    await rig.report("henrique", 405.0, north_m=10.0)
    await rig.report("kristen", 410.0, north_m=15.0)
    await rig.report("henrique", 415.0, north_m=10.0)
    await rig.report("kristen", 420.0, north_m=15.0)
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()

    # Kristen then drives away inside what would have been the grace window:
    # the pair is now known apart, but the cancelled edge must not fire.
    await rig.report("kristen", 450.0, north_m=-300.0)
    await rig.report("henrique", 460.0, north_m=10.0)
    await rig.report("kristen", 470.0, north_m=-600.0)
    await rig.report("kristen", 490.0, north_m=-800.0)
    await rig.report("henrique", 495.0, north_m=10.0)

    assert rig.device.calls == []


@pytest.mark.asyncio
async def test_accuracy_deferred_edge_cancelled_when_pair_arrives_together_then_separates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An edge deferred for accuracy must also die once togetherness is known unmet.

    Staged by registering the accuracy-deferred edge directly (the pattern
    ``test_rule_evaluator_accuracy_edge_grace`` uses): with one rule, the
    geofence-edge accuracy limit equals the rule limit, so no live fix can
    land in the band that defers for accuracy.
    """
    rig = _Rig(tmp_path, monkeypatch)
    # Henrique and Kristen travel home on the same track, reporting together.
    await rig.report("kristen", 0.0, north_m=800.0)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 120.0, north_m=650.0)
    await rig.report("henrique", 120.0, north_m=650.0)
    await rig.report("kristen", 240.0, north_m=450.0)
    await rig.report("henrique", 240.0, north_m=450.0)
    await rig.report("kristen", 330.0, north_m=300.0)
    await rig.report("henrique", 330.0, north_m=300.0)
    await rig.report("kristen", 360.0, north_m=100.0)
    await rig.report("henrique", 360.0, north_m=100.0)
    await rig.report("kristen", 380.0, north_m=15.0)
    await rig.report("henrique", 380.0, north_m=10.0)
    await rig.report("kristen", 400.0, north_m=15.0)
    await rig.report("henrique", 400.0, north_m=10.0)
    assert rig.device.calls == []

    arrived_at = _T0 + 400.0
    rig.evaluator._deferred_accuracy_edges[("henrique-enters-alone", "henrique", "house", "entered")] = (
        _DeferredAccuracyEdge(
            event="entered",
            expires_at=arrived_at + 120.0,
            geofence_id="house",
            observed_at=arrived_at,
            reason="location_accuracy",
            rule_id="henrique-enters-alone",
            user_id="henrique",
        )
    )

    # The next report retries the edge: the verdict is known and unmet, so
    # the edge is cancelled even though accuracy deferred it.
    await rig.report("henrique", 420.0, north_m=10.0)
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()

    # Kristen then drives away inside what would have been the grace window:
    # the pair is now known apart, but the cancelled edge must not fire.
    await rig.report("kristen", 450.0, north_m=-300.0)
    await rig.report("henrique", 460.0, north_m=10.0)
    await rig.report("kristen", 480.0, north_m=-600.0)
    await rig.report("kristen", 500.0, north_m=-800.0)
    await rig.report("henrique", 510.0, north_m=10.0)

    assert rig.device.calls == []


@pytest.mark.asyncio
async def test_accuracy_deferred_edge_survives_known_unmet_nested_togetherness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ``users_together`` inside ``any`` does not settle the whole rule."""
    base_rule = _rule()
    inside_condition, together_condition = base_rule.conditions.all
    rule = base_rule.model_copy(
        update={
            "conditions": RuleConditionsOut(
                all=[
                    inside_condition,
                    AnyConditionsCondition(
                        type="any",
                        conditions=[
                            together_condition,
                            AfterLocalTimeCondition(type="after_local_time", time_hhmm="21:00"),
                        ],
                    ),
                ],
            ),
        },
    )
    rig = _Rig(tmp_path, monkeypatch, rule=rule)
    await rig.report("kristen", 0.0, north_m=800.0)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 120.0, north_m=650.0)
    await rig.report("henrique", 120.0, north_m=650.0)
    await rig.report("kristen", 240.0, north_m=450.0)
    await rig.report("henrique", 240.0, north_m=450.0)
    await rig.report("kristen", 330.0, north_m=300.0)
    await rig.report("henrique", 330.0, north_m=300.0)
    await rig.report("kristen", 360.0, north_m=100.0)
    await rig.report("henrique", 360.0, north_m=100.0)
    await rig.report("kristen", 380.0, north_m=15.0)
    await rig.report("henrique", 380.0, north_m=10.0)
    await rig.report("kristen", 400.0, north_m=15.0)
    await rig.report("henrique", 400.0, north_m=10.0)
    assert rig.device.calls == []

    arrived_at = _T0 + 400.0
    rig.evaluator._deferred_accuracy_edges[("henrique-enters-alone", "henrique", "house", "entered")] = (
        _DeferredAccuracyEdge(
            event="entered",
            expires_at=arrived_at + 20_000.0,
            geofence_id="house",
            observed_at=arrived_at,
            reason="location_accuracy",
            rule_id="henrique-enters-alone",
            user_id="henrique",
        )
    )

    # The nested verdict is known and failing before 21:00, but the ``any``
    # can still pass later through its time branch, so the edge survives.
    await rig.report("henrique", 420.0, north_m=10.0)
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") != ()

    # At 21:01 local, the time branch satisfies the ``any`` and the retained
    # edge fires.
    await rig.report("kristen", 13_680.0, north_m=15.0)
    await rig.report("henrique", 13_690.0, north_m=10.0)

    assert rig.device.calls == ["on"]


@pytest.mark.asyncio
async def test_persisted_outcome_tracks_deferred_then_fired(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rule_fire_state_store import list_rule_fire_states
    from app.rules_status import build_rules_status

    rig = _Rig(tmp_path, monkeypatch)
    await _drive_to_stale_peer_arrival(rig)

    record = list_rule_fire_states(rig.db)["henrique-enters-alone"]
    assert record.last_outcome == "deferred"
    assert record.last_skip_reason == "togetherness_unknown"
    assert record.last_evaluated_at == _T0 + 360.0
    assert record.last_fired_at is None

    status = build_rules_status(cache_path=rig.db, evaluator=rig.evaluator)
    (row,) = [item for item in status.rules if item.id == "henrique-enters-alone"]
    assert row.last_outcome is not None and row.last_outcome.value == "deferred"
    assert row.last_skip_reason == "togetherness_unknown"
    assert row.last_evaluated_at is not None

    await rig.report("kristen", 380.0, north_m=-800.0)
    await rig.report("henrique", 395.0, north_m=10.0)
    await rig.report("kristen", 400.0, north_m=-790.0)
    await rig.report("henrique", 415.0, north_m=10.0)
    await rig.report("kristen", 420.0, north_m=-780.0)

    assert rig.device.calls == ["on"]
    record = list_rule_fire_states(rig.db)["henrique-enters-alone"]
    assert record.last_outcome == "fired"
    assert record.last_skip_reason is None
    assert record.last_fired_at == _T0 + 420.0


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

    from app.rule_fire_state_store import list_rule_fire_states

    record = list_rule_fire_states(rig.db)["henrique-enters-alone"]
    assert record.last_outcome == "skipped"
    assert record.last_skip_reason == "deferred_edge_expired"


@pytest.mark.asyncio
async def test_persisted_outcome_records_cooldown_skip_after_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rule_fire_state_store import list_rule_fire_states

    rig = _Rig(tmp_path, monkeypatch, rule=_rule(cooldown_s=3600))
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

    # Henrique leaves long enough for a genuine re-entry, then returns while
    # the hour-long cooldown from the first fire is still running.
    await rig.report("henrique", 350.0, north_m=300.0)
    await rig.report("kristen", 360.0, north_m=-800.0)
    await rig.report("henrique", 500.0, north_m=600.0)
    await rig.report("kristen", 520.0, north_m=-800.0)
    await rig.report("henrique", 700.0, north_m=800.0)
    await rig.report("kristen", 720.0, north_m=-800.0)
    await rig.report("henrique", 740.0, north_m=600.0)
    await rig.report("kristen", 750.0, north_m=-800.0)
    await rig.report("henrique", 760.0, north_m=400.0)
    await rig.report("kristen", 770.0, north_m=-800.0)
    await rig.report("henrique", 780.0, north_m=300.0)
    await rig.report("kristen", 790.0, north_m=-800.0)
    await rig.report("henrique", 800.0, north_m=280.0)
    await rig.report("kristen", 810.0, north_m=-800.0)
    await rig.report("henrique", 820.0, north_m=150.0)

    assert rig.device.calls == ["on"]
    record = list_rule_fire_states(rig.db)["henrique-enters-alone"]
    assert record.last_outcome == "skipped"
    assert record.last_skip_reason == "cooldown"
    assert record.last_fired_at == _T0 + 320.0


@pytest.mark.asyncio
async def test_persisted_outcome_records_skip_when_pair_known_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.rule_fire_state_store import list_rule_fire_states

    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=790.0)
    await rig.report("henrique", 100.0, north_m=650.0)
    await rig.report("kristen", 100.0, north_m=640.0)
    await rig.report("henrique", 240.0, north_m=400.0)
    await rig.report("kristen", 260.0, north_m=390.0)
    await rig.report("henrique", 280.0, north_m=280.0)
    await rig.report("kristen", 285.0, north_m=270.0)
    await rig.report("kristen", 305.0, north_m=200.0)
    await rig.report("kristen", 318.0, north_m=160.0)
    await rig.report("henrique", 320.0, north_m=150.0)

    # Known together is a settled verdict: no deferral, and the skip is recorded.
    assert rig.device.calls == []
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    record = list_rule_fire_states(rig.db)["henrique-enters-alone"]
    assert record.last_outcome == "skipped"
    assert record.last_skip_reason == "conditions_not_met"
    assert record.last_skip_detail is not None and "Not travelling together" in record.last_skip_detail


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


async def test_far_away_user_with_coarse_fixes_does_not_defer_the_alone_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field case: the peer's phone reported 100 m Wi-Fi fixes from hundreds of kilometers away."""
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-400_000.0, accuracy_m=100)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 310.0, north_m=-400_000.0, accuracy_m=100)
    await rig.report("henrique", 360.0, north_m=10.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []


async def test_coarse_fixes_near_home_still_defer_the_alone_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-800.0, accuracy_m=100)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 310.0, north_m=-810.0, accuracy_m=100)
    await rig.report("henrique", 360.0, north_m=10.0)

    assert rig.device.calls == []
    assert len(rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique")) == 1


async def test_peer_re_sending_an_old_fix_far_away_does_not_defer_the_alone_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field case: the peer's phone reports on a schedule but each report repeats one old cached fix."""
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-400_000.0, accuracy_m=19)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 100.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=0.0)
    await rig.report("kristen", 300.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=0.0)
    await rig.report("henrique", 360.0, north_m=10.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
    assert rig.coordinator.schedule_request_with_reason.call_args_list == []


async def test_peer_re_sending_an_old_fix_once_that_was_never_close_settles_the_alone_arrival(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("kristen", 0.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=-5000.0)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("kristen", 300.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=0.0)
    await rig.report("henrique", 360.0, north_m=10.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()


async def test_stationary_peer_reports_older_than_the_walkback_window_still_count(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reports that prove the peer has not moved can be older than the 10 minute interpolation walkback."""
    rig = _Rig(tmp_path, monkeypatch)
    await rig.report("kristen", -2000.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=-10_000.0)
    await rig.report("kristen", -700.0, north_m=-400_000.0, accuracy_m=19, fix_at_s=-10_000.0)
    await rig.report("henrique", 0.0, north_m=800.0)
    await rig.report("henrique", 200.0, north_m=400.0)
    await rig.report("henrique", 360.0, north_m=10.0)

    assert rig.device.calls == ["on"]
    assert rig.evaluator.deferred_accuracy_edge_snapshots_for_user("henrique") == ()
