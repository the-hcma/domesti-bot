"""A dry-run rule fires as usual but only emails what it would have done."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.location_history_retention import default_location_history_retention
from app.outbound_email import rule_fire_provenance_footer
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rule_notification import (
    RULE_FIRE_DRY_RUN_HEADING,
    RULE_FIRE_DRY_RUN_INTRO_TEMPLATE,
    RULE_FIRE_JUST_FIRED_TEMPLATE,
    build_rule_notification_bodies,
    format_dry_run_action_line,
)
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_MAC = "02:00:00:00:00:09"


class _FakeKasa:
    def __init__(self) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = "192.0.2.9"
        self.host = "192.0.2.9"
        self.mac_address = _MAC
        self.identifier = _MAC
        self.preferred_label = "Hall light"
        self.calls: list[str] = []
        self._on = False

    @property
    def is_on(self) -> bool:
        return self._on

    async def turn_on(self) -> None:
        self._on = True
        self.calls.append("on")

    async def turn_off(self) -> None:
        self._on = False
        self.calls.append("off")


class _Rig:
    """One evaluator wired to a fake Kasa light and a house geofence."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rule: RuleOut) -> None:
        bundle = tmp_path / "rules.json"
        self.db = tmp_path / "discovery.sqlite"
        bundle.write_text(
            json.dumps(
                {
                    "version": 1,
                    "device_id_resolution": "preferred_label",
                    "settings_location": {
                        "lat": 41.194072,
                        "lon": -73.8883254,
                        "timezone": "America/New_York",
                        "home_label": "Home",
                    },
                    "rules": [rule.model_dump(mode="json")],
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
        self.clock = {"now": 1_700_000_000.0}
        replace_users(
            self.db,
            [
                UserRecord(
                    user_id="henrique",
                    first_name="Test",
                    last_name="",
                    display_name="Test",
                    tracking_device_label="Phone",
                    enabled=True,
                ),
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
        self._move_sync(lat=44.0, lon=-73.0, advance_s=0.0)
        self.device = _FakeKasa()
        kasa_mgr = MagicMock(spec=KasaDeviceManager)
        kasa_mgr.switches = (self.device,)
        kasa_mgr.get_device_by_alias.return_value = None
        state = DeviceManagersState(
            kasa_mgr=cast(KasaDeviceManager, kasa_mgr),
            sonos_mgr=None,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=self.db,
            args=argparse.Namespace(),
        )
        self.evaluator = RuleEvaluator(
            cache_path=self.db,
            device_state_getter=lambda: state,
            now_fn=lambda: self.clock["now"],
        )
        self.evaluator.start_periodic_tick()

    async def enter(self) -> None:
        self._move_sync(lat=41.194085, lon=-73.888365, advance_s=400.0)
        await self.evaluator.on_location_update("henrique")

    def _move_sync(self, *, lat: float, lon: float, advance_s: float) -> None:
        self.clock["now"] += advance_s
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id="henrique",
                lat=lat,
                lon=lon,
                accuracy_m=20,
                fix_at=self.clock["now"],
                reported_at=self.clock["now"],
                source="test",
            ),
            retention=default_location_history_retention(),
        )


def _rule(*, dry_run: bool, notify: bool = True) -> RuleOut:
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
        cooldown_s=0,
        device_actions=[
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_ON,
                device_id=_MAC,
                family_id=DeviceFamilyId.KASA,
            ),
            RuleDeviceActionOut(
                action=RuleDeviceActionType.TURN_OFF,
                delay_s=600,
                device_id=_MAC,
                family_id=DeviceFamilyId.KASA,
            ),
        ],
        dry_run=dry_run,
        enabled=True,
        id="hall-light-on",
        label="Hall light on",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"] if notify else [],
        notify_on_fire=notify,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


def test_dry_run_requires_notify_on_fire() -> None:
    with pytest.raises(ValidationError, match="Expected notify_on_fire true for a dry_run rule"):
        _rule(dry_run=True, notify=False)


def test_dry_run_defaults_to_off_for_existing_rules() -> None:
    payload = _rule(dry_run=False).model_dump(mode="json")
    del payload["dry_run"]
    assert RuleOut.model_validate(payload).dry_run is False


def test_dry_run_action_line_names_the_verb_device_family_and_delay() -> None:
    now = _rule(dry_run=True).device_actions[0]
    later = _rule(dry_run=True).device_actions[1]
    assert format_dry_run_action_line(now, device_display="Hall light") == "Turn on Hall light (Kasa)"
    assert format_dry_run_action_line(later, device_display="Hall light") == "Turn off Hall light (Kasa), after 600 s"


def test_dry_run_bodies_say_nothing_changed_and_list_what_would_happen() -> None:
    rule = _rule(dry_run=True)
    plain, html = build_rule_notification_bodies(
        rule,
        cache_path=None,
        dry_run=True,
        would_do_lines=("Turn on Hall light (Kasa)",),
    )
    assert RULE_FIRE_DRY_RUN_INTRO_TEMPLATE.format(label=rule.label, rule_id=rule.id) in plain
    assert RULE_FIRE_JUST_FIRED_TEMPLATE.format(label=rule.label, rule_id=rule.id) not in plain
    assert f"{RULE_FIRE_DRY_RUN_HEADING}:\n- Turn on Hall light (Kasa)" in plain
    assert "<li>Turn on Hall light (Kasa)</li>" in html
    footer = rule_fire_provenance_footer(rule.id, dry_run=True)
    assert footer == "Sent by: domesti-bot · Rule hall-light-on (dry run)"
    assert footer in plain
    assert footer in html


@pytest.mark.asyncio
async def test_dry_run_rule_emails_what_it_would_do_and_changes_no_device(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule(dry_run=True))
    send_mock = MagicMock(return_value=RuleNotificationEmailOutcome.disabled())
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            await rig.enter()
        assert rig.device.calls == []
        assert send_mock.call_count == 1
        kwargs = send_mock.call_args.kwargs
        assert kwargs["dry_run"] is True
        assert kwargs["would_do_lines"] == (
            "Turn on Hall light (02:00:00:00:00:09) (Kasa)",
            "Turn off Hall light (02:00:00:00:00:09) (Kasa), after 600 s",
        )
        assert kwargs["device_action_outcomes"] == ()
        assert rig.evaluator._deferred_device_actions == []
    finally:
        await rig.evaluator.close()


@pytest.mark.asyncio
async def test_live_rule_still_changes_the_device_and_is_not_marked_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = _rule(dry_run=False)
    # A delayed action would defer the email until it has run; keep this one immediate.
    rig = _Rig(tmp_path, monkeypatch, rule=live.model_copy(update={"device_actions": live.device_actions[:1]}))
    send_mock = MagicMock(return_value=RuleNotificationEmailOutcome.disabled())
    try:
        with patch("app.rule_evaluator.send_rule_notification_email", send_mock):
            await rig.enter()
        assert rig.device.calls == ["on"]
        kwargs = send_mock.call_args.kwargs
        assert kwargs["dry_run"] is False
        assert kwargs["would_do_lines"] == ()
    finally:
        await rig.evaluator.close()
