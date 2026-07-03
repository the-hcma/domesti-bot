"""Hermetic tests for dwell_satisfied rule triggers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from app.api.schemas import RuleConditionsOut, RuleOut, UsersOutsideGeofenceForSCondition
from app.device_enums import RuleTrigger
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users


@pytest.mark.asyncio
async def test_dwell_satisfied_trigger_fires_when_outside_dwell_elapses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_dwell_notify_rule())
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_location_update("kristen")

    send_mock.assert_called_once()
    assert evaluator.fire_state_for_rule("away-dwell-notify").last_fired_at == (
        clock["now"]
    )


@pytest.mark.asyncio
async def test_dwell_satisfied_trigger_fires_once_per_away_episode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, _away_dwell_notify_rule(cooldown_s=0))
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))

    clock = {"now": 1_700_000_000.0}
    _seed_presence_db(db, now=clock["now"])
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    await evaluator.on_location_update("henrique")
    await evaluator.on_location_update("kristen")
    clock["now"] += 1300.0

    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await evaluator.on_location_update("henrique")
        clock["now"] += 60.0
        await evaluator.on_location_update("kristen")

    assert send_mock.call_count == 1


def _away_dwell_notify_rule(*, cooldown_s: int = 0) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                UsersOutsideGeofenceForSCondition(
                    type="users_outside_geofence_for_s",
                    geofence_id="house",
                    min_outside_s=1200,
                    user_ids=["henrique", "kristen"],
                ),
            ],
        ),
        cooldown_s=cooldown_s,
        device_actions=[],
        enabled=True,
        id="away-dwell-notify",
        label="Away dwell notify",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DWELL_SATISFIED],
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
            UserRecord(
                user_id="kristen",
                first_name="Kristen",
                last_name="",
                display_name="Kristen",
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
    for user_id in ("henrique", "kristen"):
        upsert_user_location(
            db,
            UserLocationRecord(
                user_id=user_id,
                lat=44.0,
                lon=-73.0,
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
