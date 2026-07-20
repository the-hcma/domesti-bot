"""Hermetic tests for scheduled-rule periodic wake timing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.schemas import (
    RuleConditionsOut,
    RuleDeviceActionOut,
    RuleOut,
    UsersInsideGeofenceCondition,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType, RuleTrigger
from app.rule_evaluator import (
    _RULE_EVALUATOR_MAX_IDLE_S,
    _RULE_EVALUATOR_MIN_SLEEP_S,
    RuleEvaluator,
)


def _write_bundle(path: Path, *rules: RuleOut) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "preferred_label",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _scheduled_rule(*, rule_id: str, cooldown_s: int = 0) -> RuleOut:
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
        fire_once_per_local_day=False,
        id=rule_id,
        label=rule_id,
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        schedule_cron="30 8 * * 1-5",
        triggers=[RuleTrigger.SCHEDULED],
    )


def _evaluator_for_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *rules: RuleOut,
) -> tuple[dict[str, float], RuleEvaluator]:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    _write_bundle(bundle, *rules)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    clock = {"now": 1_700_000_000.0}
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )
    return clock, evaluator


def test_periodic_wake_delay_targets_next_evaluate_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, evaluator = _evaluator_for_rules(
        tmp_path,
        monkeypatch,
        _scheduled_rule(rule_id="scheduled-inside"),
    )
    now = clock["now"]
    evaluator._rule_state["scheduled-inside"].next_evaluate_at = now + 30.0

    assert evaluator._periodic_wake_delay_s(now) == pytest.approx(30.0)


def test_periodic_wake_delay_caps_at_max_idle_without_scheduled_rules(
    tmp_path: Path,
) -> None:
    db = tmp_path / "discovery.sqlite"
    clock = {"now": 1_700_000_000.0}
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: clock["now"],
    )

    assert evaluator._periodic_wake_delay_s(clock["now"]) == _RULE_EVALUATOR_MAX_IDLE_S


def test_periodic_wake_delay_uses_earliest_of_two_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, evaluator = _evaluator_for_rules(
        tmp_path,
        monkeypatch,
        _scheduled_rule(rule_id="scheduled-sooner"),
        _scheduled_rule(rule_id="scheduled-later"),
    )
    now = clock["now"]
    evaluator._rule_state["scheduled-sooner"].next_evaluate_at = now + 12.0
    evaluator._rule_state["scheduled-later"].next_evaluate_at = now + 45.0

    assert evaluator._periodic_wake_delay_s(now) == pytest.approx(12.0)


def test_periodic_wake_delay_when_due_is_past_returns_min_sleep(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, evaluator = _evaluator_for_rules(
        tmp_path,
        monkeypatch,
        _scheduled_rule(rule_id="scheduled-inside"),
    )
    now = clock["now"]
    evaluator._rule_state["scheduled-inside"].next_evaluate_at = now - 5.0

    assert evaluator._periodic_wake_delay_s(now) == _RULE_EVALUATOR_MIN_SLEEP_S


def test_earliest_scheduled_evaluate_at_seeds_missing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, evaluator = _evaluator_for_rules(
        tmp_path,
        monkeypatch,
        _scheduled_rule(rule_id="scheduled-inside"),
    )
    runtime = evaluator._rule_state["scheduled-inside"]
    runtime.next_evaluate_at = None
    now = clock["now"]

    earliest = evaluator._earliest_scheduled_evaluate_at(now)

    assert earliest is not None
    assert earliest >= now
    assert runtime.next_evaluate_at == earliest
