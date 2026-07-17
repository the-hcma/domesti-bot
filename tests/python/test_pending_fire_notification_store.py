"""Hermetic tests for pending rule-fire notification persistence (issue #506)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import RulePendingFireNotification
from app.db.session import discovery_write
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.pending_fire_notification_store import (
    append_pending_fire_notification_outcomes,
    delete_pending_fire_notification,
    delete_pending_fire_notifications_for_rule,
    get_pending_fire_notification,
    insert_pending_fire_notification,
    list_pending_fire_notifications,
    mark_pending_fire_notifications_cancelled_for_rule,
)
from app.rule_device_action_outcome import RuleDeviceActionOutcome


def _outcome(
    *,
    action: RuleDeviceActionType = RuleDeviceActionType.TURN_OFF,
    completed_at: float = 1_700_000_000.0,
    device_id: str = "HDHomeRun tuner",
) -> RuleDeviceActionOutcome:
    return RuleDeviceActionOutcome(
        action=action,
        after_state="off" if action == RuleDeviceActionType.TURN_OFF else "on",
        before_state="on" if action == RuleDeviceActionType.TURN_OFF else "off",
        completed_at=completed_at,
        device_id=device_id,
        error=None,
        family_id=DeviceFamilyId.KASA,
        probable=False,
        succeeded=True,
    )


def test_insert_get_and_append_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    fire_at = 1_700_000_000.0
    insert_pending_fire_notification(
        db,
        fire_at=fire_at,
        notification_detail="Nightly power cycle.",
        outcomes=(_outcome(),),
        rule_id="hdhomerun-nightly-power-cycle",
    )
    row = get_pending_fire_notification(
        db,
        fire_at=fire_at,
        rule_id="hdhomerun-nightly-power-cycle",
    )
    assert row is not None
    assert row.notification_detail == "Nightly power cycle."
    assert len(row.outcomes) == 1
    assert row.cancelled_remaining is False

    append_pending_fire_notification_outcomes(
        db,
        fire_at=fire_at,
        outcomes=(_outcome(action=RuleDeviceActionType.TURN_ON, completed_at=fire_at + 60.0),),
        rule_id="hdhomerun-nightly-power-cycle",
    )
    updated = get_pending_fire_notification(
        db,
        fire_at=fire_at,
        rule_id="hdhomerun-nightly-power-cycle",
    )
    assert updated is not None
    assert len(updated.outcomes) == 2
    assert updated.outcomes[1].action == RuleDeviceActionType.TURN_ON


def test_list_skips_corrupt_outcomes_json(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    fire_at = 1_700_000_000.0
    insert_pending_fire_notification(
        db,
        fire_at=fire_at,
        notification_detail=None,
        outcomes=(_outcome(),),
        rule_id="good",
    )
    insert_pending_fire_notification(
        db,
        fire_at=fire_at + 1.0,
        notification_detail=None,
        outcomes=(_outcome(),),
        rule_id="bad",
    )

    def _corrupt(session: Session) -> None:
        session.execute(
            update(RulePendingFireNotification)
            .where(RulePendingFireNotification.rule_id == "bad")
            .values(outcomes_json='{"not":"a-list"}')
        )

    discovery_write(db, _corrupt)
    records = list_pending_fire_notifications(db)
    assert len(records) == 1
    assert records[0].rule_id == "good"


def test_mark_cancelled_and_delete(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    fire_at = 1_700_000_000.0
    insert_pending_fire_notification(
        db,
        fire_at=fire_at,
        notification_detail=None,
        outcomes=(_outcome(),),
        rule_id="a",
    )
    insert_pending_fire_notification(
        db,
        fire_at=fire_at + 1.0,
        notification_detail=None,
        outcomes=(_outcome(),),
        rule_id="b",
    )
    marked = mark_pending_fire_notifications_cancelled_for_rule(db, "a")
    assert len(marked) == 1
    assert marked[0].cancelled_remaining is True
    delete_pending_fire_notification(db, fire_at=fire_at, rule_id="a")
    assert get_pending_fire_notification(db, fire_at=fire_at, rule_id="a") is None
    delete_pending_fire_notifications_for_rule(db, "b")
    assert list_pending_fire_notifications(db) == []
