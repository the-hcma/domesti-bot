"""The sample garage entry rules open one door as the car crosses the geofence (dry run, nothing moves)."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import RuleOut
from app.automation_rules_loader import load_automation_rules_bundle
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_EXAMPLE = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
_HENRIQUE_ALONE = "garage-entry-alone-open-henriques-side"
_HOME = (41.194072, -73.888325)
_KRISTEN_ALONE = "garage-entry-alone-open-kristens-side"
_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_REPORT_EVERY_S = 10.0
_SPEED_MPS = 20.0
_T0 = 1_700_000_000.0
_TOGETHER = "garage-entry-together-open-kristens-side"


class _Door:
    def __init__(self, identifier: str, label: str) -> None:
        self.identifier = identifier
        self.mac_address = None
        self.door_key = identifier
        self.preferred_label = label
        self.is_open = False


class _Rig:
    """An evaluator loaded with the example garage entry rules, enabled, and two closed doors."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(_EXAMPLE))
        example = load_automation_rules_bundle()
        rules = [
            rule.model_copy(update={"enabled": True})
            for rule in example.rules
            if rule.id in (_TOGETHER, _HENRIQUE_ALONE, _KRISTEN_ALONE)
        ]
        assert len(rules) == 3
        bundle = tmp_path / "rules.json"
        bundle.write_text(
            json.dumps(
                {
                    "version": 1,
                    "device_id_resolution": "mac",
                    "settings_location": {
                        "lat": _HOME[0],
                        "lon": _HOME[1],
                        "timezone": "America/New_York",
                        "home_label": "Home",
                    },
                    "rules": [rule.model_dump(mode="json") for rule in rules],
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
        self.db = tmp_path / "discovery.sqlite"
        self.clock = {"now": _T0}
        replace_users(
            self.db,
            [
                UserRecord(
                    user_id=user_id,
                    first_name=user_id.title(),
                    last_name="",
                    display_name=user_id.title(),
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
                    center_lat=_HOME[0],
                    center_lon=_HOME[1],
                    radius_m=250,
                    enabled=True,
                    owntracks_rid=None,
                ),
            ],
        )
        mgr = MagicMock(spec=GotailwindDeviceManager)
        mgr.doors = (
            _Door("02:00:00:00:03:01:left", "Main garage left"),
            _Door("02:00:00:00:03:01:right", "Main garage right"),
        )
        state = DeviceManagersState(
            androidtv_mgr=None,
            args=argparse.Namespace(),
            cache_path=self.db,
            ep1_mgr=None,
            kasa_mgr=MagicMock(spec=KasaDeviceManager),
            sonos_mgr=None,
            tailwind_mgr=cast(GotailwindDeviceManager, mgr),
            vizio_mgr=None,
        )
        self.evaluator = RuleEvaluator(
            cache_path=self.db,
            device_state_getter=lambda: state,
            now_fn=lambda: self.clock["now"],
        )
        self.opened: list[tuple[str, str]] = []
        self.opened_at: list[float] = []
        self.details: list[str | None] = []
        self._send = MagicMock(side_effect=self._record, return_value=RuleNotificationEmailOutcome.disabled())
        self._patch = patch("app.rule_evaluator.send_rule_notification_email", self._send)
        self._patch.start()

    def _record(self, *_args: object, **kwargs: object) -> RuleNotificationEmailOutcome:
        rule = cast(RuleOut, kwargs["rule"])
        assert kwargs["dry_run"] is True
        lines = cast(tuple[str, ...], kwargs["would_do_lines"])
        self.opened.extend((rule.id, line) for line in lines)
        self.opened_at.extend(self.clock["now"] for _ in lines)
        self.details.append(cast(str | None, kwargs["notification_detail"]))
        return RuleNotificationEmailOutcome.disabled()

    async def close(self) -> None:
        self._patch.stop()
        await self.evaluator.close()

    async def drive(self, schedule: list[tuple[float, str, tuple[float, float]]]) -> None:
        """Report each ``(time, user, position)`` in time order, with the clock at the report time."""
        for at, user_id, position in sorted(schedule):
            self.clock["now"] = at
            upsert_user_location(
                self.db,
                UserLocationRecord(
                    user_id=user_id,
                    lat=position[0],
                    lon=position[1],
                    accuracy_m=5,
                    fix_at=at,
                    reported_at=at,
                    source="test",
                ),
                retention=default_location_history_retention(),
            )
            await self.evaluator.on_location_update(user_id)


def _approach(
    user_id: str,
    *,
    arrives_at: float,
    every_s: float,
    offset_s: float,
    lateral_m: float,
    until: float,
) -> list[tuple[float, str, tuple[float, float]]]:
    """Reports of a phone in a car that has been driving toward the house for the last 10 minutes."""
    count = int((until - _T0) // every_s)
    return [
        (at, user_id, _car(at, arrives_at=arrives_at, lateral_m=lateral_m))
        for at in (_T0 + offset_s + index * every_s for index in range(count + 1))
        if at <= until
    ]


def _car(at: float, *, arrives_at: float, lateral_m: float = 0.0) -> tuple[float, float]:
    """A car driving north toward the house at 20 m/s, reaching its center at ``arrives_at``."""
    south_m = max(0.0, _SPEED_MPS * (arrives_at - at))
    lat = _HOME[0] - south_m / _M_PER_DEG
    lon = _HOME[1] + lateral_m / (_M_PER_DEG * math.cos(math.radians(_HOME[0])))
    return lat, lon


@pytest.mark.asyncio
async def test_two_phones_in_one_car_open_kristens_side_once_at_the_geofence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    try:
        await rig.drive(
            _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            )
            + _approach("kristen", arrives_at=arrives_at, every_s=19.0, offset_s=3.0, lateral_m=3.0, until=until),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        assert rig.opened[0][1].startswith("Open Main garage right")
        (detail,) = rig.details
        assert detail is not None
        assert detail.startswith("Henrique and Kristen: confidence ")
        assert " m apart at the latest shared fix" in detail
        crossing = arrives_at - 250.0 / _SPEED_MPS
        assert crossing <= rig.opened_at[0] <= crossing + _REPORT_EVERY_S + 2.0
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_kristen_arriving_alone_opens_her_side_when_henrique_is_far_away(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    far_away = (_HOME[0] - 0.2, _HOME[1])
    try:
        await rig.drive(
            [(_T0 + 30.0 * index, "henrique", far_away) for index in range(int((until - _T0) // 30.0) + 1)]
            + _approach("kristen", arrives_at=arrives_at, every_s=19.0, offset_s=3.0, lateral_m=0.0, until=until),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_KRISTEN_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage right")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_henrique_arriving_alone_opens_his_side_when_kristen_is_already_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    at_home = _car(_T0, arrives_at=_T0, lateral_m=20.0)
    try:
        await rig.drive(
            [(_T0 + 30.0 * index, "kristen", at_home) for index in range(int((until - _T0) // 30.0) + 1)]
            + _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            ),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_passenger_phone_that_went_quiet_leaves_the_entry_undecided_and_opens_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    try:
        await rig.drive(
            _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            )
            + _approach("kristen", arrives_at=arrives_at, every_s=19.0, offset_s=3.0, lateral_m=3.0, until=_T0 + 600.0),
        )
        assert rig.opened == []
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_slow_driver_phone_delays_the_together_rule_to_the_drivers_own_entry_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The together rule listens only to one user's entry, so it waits for that phone's first inside fix."""
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    crossing = arrives_at - 250.0 / _SPEED_MPS
    try:
        await rig.drive(
            _approach("henrique", arrives_at=arrives_at, every_s=30.0, offset_s=0.0, lateral_m=0.0, until=until)
            + _approach(
                "kristen", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=1.0, lateral_m=3.0, until=until
            ),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        assert crossing < rig.opened_at[0] <= crossing + 30.0
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_home_phone_with_no_recent_fix_leaves_an_arrival_alone_undecided_and_opens_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Known gap: a phone that stopped reporting cannot be told apart from one in the car, so nothing opens."""
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    at_home = _car(_T0, arrives_at=_T0, lateral_m=20.0)
    try:
        await rig.drive(
            [(_T0, "kristen", at_home)]
            + _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            ),
        )
        assert rig.opened == []
    finally:
        await rig.close()
