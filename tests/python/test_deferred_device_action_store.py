"""Hermetic tests for the persisted delayed device-action store (issue #485)."""

from __future__ import annotations

from pathlib import Path

from app.api.schemas import RuleDeviceActionOut
from app.deferred_device_action_store import (
    delete_deferred_device_actions,
    delete_deferred_device_actions_for_rule,
    insert_deferred_device_action,
    list_deferred_device_actions,
)
from app.device_enums import DeviceFamilyId, RuleDeviceActionType


def _action() -> RuleDeviceActionOut:
    return RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_ON,
        delay_s=60,
        device_id="HDHomeRun tuner",
        family_id=DeviceFamilyId.KASA,
    )


def test_insert_and_list_round_trips_action(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    row_id = insert_deferred_device_action(
        db,
        action=_action(),
        due_at=1_700_000_060.0,
        fire_at=1_700_000_000.0,
        rule_id="hdhomerun-nightly-power-cycle",
    )
    records = list_deferred_device_actions(db)
    assert len(records) == 1
    record = records[0]
    assert record.row_id == row_id
    assert record.rule_id == "hdhomerun-nightly-power-cycle"
    assert record.due_at == 1_700_000_060.0
    assert record.fire_at == 1_700_000_000.0
    assert record.action.action == RuleDeviceActionType.TURN_ON
    assert record.action.device_id == "HDHomeRun tuner"
    assert record.action.family_id == DeviceFamilyId.KASA
    assert record.action.delay_s == 60


def test_list_orders_by_due_at(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    insert_deferred_device_action(db, action=_action(), due_at=200.0, fire_at=0.0, rule_id="b")
    insert_deferred_device_action(db, action=_action(), due_at=100.0, fire_at=0.0, rule_id="a")
    records = list_deferred_device_actions(db)
    assert [record.due_at for record in records] == [100.0, 200.0]


def test_delete_by_row_ids_removes_only_targeted(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    first = insert_deferred_device_action(db, action=_action(), due_at=100.0, fire_at=0.0, rule_id="a")
    insert_deferred_device_action(db, action=_action(), due_at=200.0, fire_at=0.0, rule_id="b")
    delete_deferred_device_actions(db, [first])
    remaining = list_deferred_device_actions(db)
    assert len(remaining) == 1
    assert remaining[0].rule_id == "b"


def test_delete_for_rule_removes_all_rows_for_rule(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    insert_deferred_device_action(db, action=_action(), due_at=100.0, fire_at=0.0, rule_id="a")
    insert_deferred_device_action(db, action=_action(), due_at=200.0, fire_at=0.0, rule_id="a")
    insert_deferred_device_action(db, action=_action(), due_at=300.0, fire_at=0.0, rule_id="b")
    delete_deferred_device_actions_for_rule(db, "a")
    remaining = list_deferred_device_actions(db)
    assert len(remaining) == 1
    assert remaining[0].rule_id == "b"
