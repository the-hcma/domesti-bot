"""The sample garage entry rules open one door as the car crosses the geofence (dry run, nothing moves)."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import RuleConditionsOut, RuleOut, UsersTogetherCondition
from app.automation_rules_loader import load_automation_rules_bundle
from app.device_enums import RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import (
    DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
    DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
    LocationHistoryRetention,
)
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_conditions import evaluate_rule
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users, set_user_home_wifi

# The simulated clock is years in the past, so the default 24 hour window would prune every reading but the newest
# 20; production keeps the whole day, and the recent-closeness rule reads the last few minutes of both tracks.
_FULL_HISTORY = LocationHistoryRetention(
    max_age_s=DEFAULT_LOCATION_HISTORY_MAX_AGE_S,
    min_keep_count=DEFAULT_LOCATION_HISTORY_MIN_KEEP_COUNT,
    unlimited=True,
)
_EXAMPLE = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "togetherness"
_HOME_SSID = "HomeNet"
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


@dataclass(frozen=True)
class _Report:
    """One stored report: where the phone says it is, how good the fix is and what network it was on."""

    position: tuple[float, float]
    reported_at: float
    user_id: str
    accuracy_m: int | None = 5
    connection_type: str | None = None
    fix_at: float = 0.0
    wifi_ssid: str | None = None

    def __post_init__(self) -> None:
        if self.fix_at == 0.0:
            object.__setattr__(self, "fix_at", self.reported_at)


class _Rig:
    """An evaluator loaded with the example garage entry rules, enabled, and two closed doors."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, home_wifi_ssid: str | None = None) -> None:
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
        if home_wifi_ssid is not None:
            for user_id in ("henrique", "kristen"):
                set_user_home_wifi(self.db, user_id, wifi_ssid=home_wifi_ssid)
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
                retention=_FULL_HISTORY,
            )
            await self.evaluator.on_location_update(user_id)

    async def togetherness_at(self, report: _Report) -> tuple[bool, bool]:
        """Whether the pair is clearly alone (apart) and clearly together, as the rules would see it at ``report``."""
        ctx = await self.evaluator._build_evaluation_context(now=datetime.fromtimestamp(report.reported_at, tz=UTC))
        apart = evaluate_rule(_users_together_rule(expected=False), ctx).conditions[0].met
        together = evaluate_rule(_users_together_rule(expected=True), ctx).conditions[0].met
        return bool(apart), bool(together)

    async def drive_reports(self, reports: list[_Report]) -> None:
        """Report each stored fix in report-time order, with the clock at the report time."""
        for report in sorted(reports, key=lambda item: item.reported_at):
            self.clock["now"] = report.reported_at
            upsert_user_location(
                self.db,
                UserLocationRecord(
                    user_id=report.user_id,
                    lat=report.position[0],
                    lon=report.position[1],
                    accuracy_m=report.accuracy_m,
                    connection_type=report.connection_type,
                    fix_at=report.fix_at,
                    reported_at=report.reported_at,
                    source="test",
                    wifi_ssid=report.wifi_ssid,
                ),
                retention=_FULL_HISTORY,
            )
            await self.evaluator.on_location_update(report.user_id)


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
            # Kristen has been home for a while: her first report is well before Henrique's drive.
            [
                (_T0 - 600.0 + 30.0 * index, "kristen", at_home)
                for index in range(int((until - _T0 + 600.0) // 30.0) + 1)
            ]
            + _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            ),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_passenger_phone_that_went_quiet_shortly_before_arrival_still_opens_the_together_door(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Field case 2026-11-04 20:14: the passenger's phone went quiet about 90 s before the crossing.

    The pair was close for the whole last minute before the silence, so the verdict is carried forward (within the
    180 s carry window) instead of staying unknown until the deferred edge expires.
    """
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
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        assert rig.opened[0][1].startswith("Open Main garage right")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_passenger_phone_quiet_for_longer_than_the_window_counts_as_not_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted trade-off: no closeness for 5 minutes means not together, so the driver's side opens."""
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    try:
        await rig.drive(
            _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            )
            + _approach("kristen", arrives_at=arrives_at, every_s=19.0, offset_s=3.0, lateral_m=3.0, until=_T0 + 300.0),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
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
async def test_a_home_phone_with_no_recent_fix_still_lets_an_arrival_alone_open_the_drivers_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A phone that stopped reporting at home has not been close to the arriving car for minutes: they are apart."""
    rig = _Rig(tmp_path, monkeypatch)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    at_home = _car(_T0, arrives_at=_T0, lateral_m=20.0)
    try:
        await rig.drive(
            [(_T0 - 600.0, "kristen", at_home)]
            + _approach(
                "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
            ),
        )
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
        crossing = arrives_at - 250.0 / _SPEED_MPS
        assert crossing <= rig.opened_at[0] <= crossing + _REPORT_EVERY_S + 2.0
    finally:
        await rig.close()


def _fixture_reports(name: str) -> tuple[list[_Report], float]:
    """The reports of a recorded arrival and the report time of the first fix inside the geofence."""
    data = json.loads((_FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    columns = data["columns"]
    reports = []
    for row in data["rows"]:
        record = dict(zip(columns, row, strict=True))
        reports.append(
            _Report(
                accuracy_m=record["accuracy_m"],
                connection_type=record["connection_type"],
                fix_at=record["fix_at"],
                position=(record["lat"], record["lon"]),
                reported_at=record["reported_at"],
                user_id=record["user"],
                wifi_ssid=record["wifi_ssid"],
            )
        )
    return reports, data["crossing_reported_at"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "expected_rule", "expected_line"),
    [
        # 2026-11-06 21:23 EST: the peer's phone re-sent a 32-hour-old 100 m fix from the home Wi-Fi (domesti-bot#940).
        ("nov6_alone_arrival_with_stale_coarse_peer", _HENRIQUE_ALONE, "Open Main garage left"),
        ("oct13_together_arrival_evening", _TOGETHER, "Open Main garage right"),
        ("oct13_alone_arrival_with_peer_home_on_wifi", _HENRIQUE_ALONE, "Open Main garage left"),
    ],
)
async def test_a_recorded_arrival_opens_exactly_the_right_door_within_the_grace(
    name: str,
    expected_rule: str,
    expected_line: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replays the stored reports of both phones through the real evaluator, report by report."""
    reports, crossing = _fixture_reports(name)
    rig = _Rig(tmp_path, monkeypatch, home_wifi_ssid=_HOME_SSID)
    try:
        await rig.drive_reports(reports)
        # The trace starts an hour earlier so the phones' recent reports are all there; only what follows the
        # crossing is judged (an earlier, separate arrival in the window may legitimately have opened a door).
        after = [
            (rule_id, line, at)
            for (rule_id, line), at in zip(rig.opened, rig.opened_at, strict=True)
            if at >= crossing - 1.0
        ]
        assert [rule_id for rule_id, _line, _at in after] == [expected_rule]
        assert after[0][1].startswith(expected_line)
        assert crossing <= after[0][2] <= crossing + 120.0
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "phones_were_together"),
    [
        # 2026-10-13 17:04 EDT: one car, and one phone joined the home Wi-Fi in the driveway 90 s after the crossing.
        # The evaluator's bounce guard suppresses this crossing (the car had been out of the geofence for 200 s), so
        # the pair verdict is what is pinned here, not a door.
        ("oct13_together_arrival_with_driveway_wifi_join", True),
        ("oct13_together_arrival_evening", True),
        ("nov6_alone_arrival_with_stale_coarse_peer", False),
        ("oct13_alone_arrival_with_peer_home_on_wifi", False),
    ],
)
async def test_the_pair_verdict_after_a_recorded_crossing_never_contradicts_what_the_phones_did(
    name: str,
    phones_were_together: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At every report from just before the crossing to 3 minutes after it, the rules must not be told the opposite."""
    reports, crossing = _fixture_reports(name)
    rig = _Rig(tmp_path, monkeypatch, home_wifi_ssid=_HOME_SSID)
    timeline: list[tuple[float, bool, bool]] = []
    try:
        for report in sorted(reports, key=lambda item: item.reported_at):
            await rig.drive_reports([report])
            offset = report.reported_at - crossing
            if -30.0 <= offset <= 180.0:
                apart, together = await rig.togetherness_at(report)
                timeline.append((offset, apart, together))
    finally:
        await rig.close()
    assert timeline
    wrong = [
        (offset, apart, together)
        for offset, apart, together in timeline
        if (apart if phones_were_together else together)
    ]
    assert wrong == []
    settled = [offset for offset, apart, together in timeline if (together if phones_were_together else apart)]
    assert settled
    assert min(settled) <= 120.0


@pytest.mark.asyncio
async def test_a_home_phone_re_sending_a_stale_coarse_fix_from_the_wifi_lets_an_arrival_alone_open_the_drivers_side(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """domesti-bot#940: both rules waited out their grace because the home phone had no good fix at all."""
    rig = _Rig(tmp_path, monkeypatch, home_wifi_ssid=_HOME_SSID)
    arrives_at = _T0 + 700.0
    until = arrives_at + 120.0
    crossing = arrives_at - 250.0 / _SPEED_MPS
    stale_fix_at = _T0 - 117_800.0
    home_reports = [
        _Report(
            accuracy_m=100,
            connection_type="w",
            fix_at=stale_fix_at,
            position=_HOME,
            reported_at=crossing - age,
            user_id="kristen",
            wifi_ssid=_HOME_SSID,
        )
        for age in (246.0, 1_146.0, 2_046.0)
    ]
    drive = [
        _Report(position=position, reported_at=at, user_id="henrique", accuracy_m=5, connection_type="m")
        for at, _user, position in _approach(
            "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
        )
    ]
    try:
        await rig.drive_reports([*home_reports, *drive])
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
        assert crossing <= rig.opened_at[0] <= crossing + _REPORT_EVERY_S + 2.0
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_phone_joining_the_home_wifi_before_the_pair_settles_still_opens_only_the_together_door(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """domesti-bot#940: one car, and the driver's phone joins the home Wi-Fi while the pair is still unknown.

    The passenger's phone starts reporting only 15 s before the crossing, so the pair is unknown when the driver
    crosses and the rules wait out their grace. The driver's phone joins the home Wi-Fi on its first report at
    least 10 s after the crossing (12.5 s, on the 10 s report grid), right next to the passenger's phone, which is
    still on mobile data. That report is no evidence that the pair was apart, so the alone door must not open and
    the together door opens once the pair settles.
    """
    rig = _Rig(tmp_path, monkeypatch, home_wifi_ssid=_HOME_SSID)
    arrives_at = _T0 + 700.0
    until = arrives_at + 150.0
    crossing = arrives_at - 250.0 / _SPEED_MPS
    drive = [
        _Report(
            accuracy_m=5,
            connection_type="w" if at >= crossing + 10.0 else "m",
            position=position,
            reported_at=at,
            user_id="henrique",
            wifi_ssid=_HOME_SSID if at >= crossing + 10.0 else None,
        )
        for at, _user, position in _approach(
            "henrique", arrives_at=arrives_at, every_s=_REPORT_EVERY_S, offset_s=0.0, lateral_m=0.0, until=until
        )
    ]
    passenger = [
        _Report(
            accuracy_m=5,
            connection_type="m",
            position=_car(at, arrives_at=arrives_at, lateral_m=3.0),
            reported_at=at,
            user_id="kristen",
        )
        for at in (crossing - 15.0 + _REPORT_EVERY_S * index for index in range(int((until - crossing) // 10.0) + 2))
        if at <= until
    ]
    try:
        await rig.drive_reports([*drive, *passenger])
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        assert rig.opened[0][1].startswith("Open Main garage right")
        assert crossing + 10.0 < rig.opened_at[0] <= crossing + 120.0
    finally:
        await rig.close()


def _users_together_rule(*, expected: bool) -> RuleOut:
    """A rule holding only the pair condition, so its status row is the verdict the garage entry rules wait on."""
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[UsersTogetherCondition(type="users_together", user_ids=["henrique", "kristen"], expected=expected)]
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="timeline",
        label="Timeline",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )
