"""The sample garage arrival rules open exactly one door per arrival (dry run, nothing moves)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest

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
_TOGETHER = "garage-arrival-together-open-kristens-side"
_HENRIQUE_ALONE = "garage-arrival-alone-open-henriques-side"
_KRISTEN_ALONE = "garage-arrival-alone-open-kristens-side"
_INSIDE = (41.194085, -73.888365)
_OUTSIDE = (44.0, -73.0)


class _Door:
    def __init__(self, identifier: str, label: str) -> None:
        self.identifier = identifier
        self.mac_address = None
        self.door_key = identifier
        self.preferred_label = label
        self.is_open = False


class _Rig:
    """An evaluator loaded with the example garage arrival rules, enabled, and two closed doors."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        only: tuple[str, ...] = (_TOGETHER, _HENRIQUE_ALONE, _KRISTEN_ALONE),
    ) -> None:
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(_EXAMPLE))
        example = load_automation_rules_bundle()
        rules = [
            rule.model_copy(update={"enabled": True})
            for rule in example.rules
            if rule.id.startswith("garage-") and rule.id in only
        ]
        assert sorted(rule.id for rule in rules) == sorted(only)
        bundle = tmp_path / "rules.json"
        bundle.write_text(
            json.dumps(
                {
                    "version": 1,
                    "device_id_resolution": "mac",
                    "settings_location": {
                        "lat": 41.194072,
                        "lon": -73.8883254,
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
        self.clock = {"now": 1_700_000_000.0}
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
                    center_lat=41.194072,
                    center_lon=-73.888325,
                    radius_m=250,
                    enabled=True,
                    owntracks_rid=None,
                ),
            ],
        )
        for user_id in ("henrique", "kristen"):
            self._report(user_id, _OUTSIDE)
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
        self._send = MagicMock(side_effect=self._record, return_value=RuleNotificationEmailOutcome.disabled())
        self._patch = patch("app.rule_evaluator.send_rule_notification_email", self._send)
        self._patch.start()
        self.clock["now"] += 400.0

    def _record(self, *_args: object, **kwargs: object) -> RuleNotificationEmailOutcome:
        rule = kwargs["rule"]
        assert kwargs["dry_run"] is True
        lines = cast(tuple[str, ...], kwargs["would_do_lines"])
        self.opened.extend((getattr(rule, "id"), line) for line in lines)
        return RuleNotificationEmailOutcome.disabled()

    def _report(self, user_id: str, position: tuple[float, float]) -> None:
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id=user_id,
                lat=position[0],
                lon=position[1],
                accuracy_m=20,
                fix_at=self.clock["now"],
                reported_at=self.clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )

    async def arrive(self, user_id: str, *, after_s: float) -> None:
        await self.report(user_id, _INSIDE, after_s=after_s)

    async def close(self) -> None:
        self._patch.stop()
        await self.evaluator.close()

    async def report(self, user_id: str, position: tuple[float, float], *, after_s: float) -> None:
        self.clock["now"] += after_s
        self._report(user_id, position)
        await self.evaluator.on_location_update(user_id)

    async def ping(self, user_id: str, *, after_s: float) -> None:
        """Another report from where the user already is."""
        await self.report(user_id, _INSIDE, after_s=after_s)


@pytest.mark.asyncio
async def test_arriving_together_opens_kristens_side_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.arrive("kristen", after_s=30.0)
        await rig.ping("henrique", after_s=40.0)
        await rig.ping("kristen", after_s=10.0)
        await rig.ping("henrique", after_s=300.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        assert rig.opened[0][1].startswith("Open Main garage right")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_henrique_arriving_alone_opens_his_side_after_a_minute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.ping("henrique", after_s=30.0)
        assert rig.opened == []
        await rig.ping("henrique", after_s=40.0)
        await rig.ping("henrique", after_s=300.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage left")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_kristen_arriving_alone_opens_her_side_after_a_minute(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("kristen", after_s=0.0)
        await rig.ping("kristen", after_s=30.0)
        assert rig.opened == []
        await rig.ping("kristen", after_s=40.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_KRISTEN_ALONE]
        assert rig.opened[0][1].startswith("Open Main garage right")
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_second_person_arriving_after_the_first_already_got_a_door_opens_nothing_more(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.ping("henrique", after_s=70.0)
        await rig.arrive("kristen", after_s=20.0)
        await rig.ping("kristen", after_s=70.0)
        await rig.ping("henrique", after_s=10.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_HENRIQUE_ALONE]
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_together_rule_alone_still_fires_without_any_dwell_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Arrival times are only recorded for rules that need them; this one must not
    # depend on a dwell rule being loaded alongside it.
    rig = _Rig(tmp_path, monkeypatch, only=(_TOGETHER,))
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.arrive("kristen", after_s=30.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_staying_home_after_the_other_person_leaves_does_not_open_a_door(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.arrive("kristen", after_s=30.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        await rig.report("henrique", _OUTSIDE, after_s=120.0)
        await rig.ping("kristen", after_s=600.0)
        await rig.ping("kristen", after_s=600.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
    finally:
        await rig.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("leaver", ["henrique", "kristen"])
async def test_one_person_leaving_soon_after_a_joint_arrival_opens_no_second_door(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    leaver: str,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    stayer = "kristen" if leaver == "henrique" else "henrique"
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.arrive("kristen", after_s=30.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
        await rig.report(leaver, _OUTSIDE, after_s=70.0)
        # The one who stayed reports again well inside their 60 to 300 s window.
        await rig.ping(stayer, after_s=50.0)
        await rig.ping(stayer, after_s=60.0)
        assert [rule_id for rule_id, _line in rig.opened] == [_TOGETHER]
    finally:
        await rig.close()


@pytest.mark.asyncio
async def test_a_report_that_comes_after_the_arrival_window_opens_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch)
    try:
        await rig.arrive("henrique", after_s=0.0)
        await rig.ping("henrique", after_s=400.0)
        assert rig.opened == []
    finally:
        await rig.close()
