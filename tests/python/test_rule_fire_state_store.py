"""Hermetic tests for persisted automation rule fire state."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from app.rule_fire_state_store import list_rule_fire_states, upsert_rule_fire_state


def test_upsert_rule_fire_state_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    upsert_rule_fire_state(
        db,
        effective_schedule_cron="15 17 * * *",
        last_error=None,
        last_fired_at=1_700_000_100.0,
        next_evaluate_at=1_700_010_000.0,
        rule_id="arrive-home",
        schedule_materialized_for="2023-11-14",
        update_schedule_fields=True,
    )
    upsert_rule_fire_state(
        db,
        last_error="Device discovery still in progress; actions skipped",
        last_fired_at=1_700_000_100.0,
        rule_id="arrive-home",
    )

    rows = list_rule_fire_states(db)
    assert set(rows) == {"arrive-home"}
    record = rows["arrive-home"]
    assert record.last_fired_at == 1_700_000_100.0
    assert record.last_error == "Device discovery still in progress; actions skipped"
    assert record.effective_schedule_cron == "15 17 * * *"
    assert record.next_evaluate_at == 1_700_010_000.0
    assert record.schedule_materialized_for == "2023-11-14"


def test_upsert_rule_fire_state_upgrades_legacy_table_without_outcome_columns(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE automation_rule_state ("
            "rule_id TEXT PRIMARY KEY, effective_schedule_cron TEXT, last_error TEXT, "
            "last_fired_at FLOAT, next_evaluate_at FLOAT, schedule_materialized_for TEXT, "
            "updated_at FLOAT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO automation_rule_state (rule_id, last_fired_at, updated_at) VALUES ('arrive-home', 42.0, 1.0)"
        )

    # bootstrap_schema's additive column sync must add the outcome columns to
    # the pre-existing table instead of raising "no such column".
    upsert_rule_fire_state(
        db,
        last_error=None,
        last_evaluated_at=100.0,
        last_fired_at=42.0,
        last_outcome="skipped",
        last_skip_reason="conditions_not_met",
        rule_id="arrive-home",
        update_outcome_fields=True,
    )

    record = list_rule_fire_states(db)["arrive-home"]
    assert record.last_outcome == "skipped"
    assert record.last_skip_reason == "conditions_not_met"
    assert record.last_fired_at == 42.0


def test_upsert_rule_fire_state_round_trips_evaluation_outcome(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    upsert_rule_fire_state(
        db,
        last_error=None,
        last_evaluated_at=1_700_000_200.0,
        last_fired_at=None,
        last_outcome="skipped",
        last_skip_detail="Not travelling together: unknown, peer fix is stale",
        last_skip_reason="conditions_not_met",
        rule_id="henrique-enters-alone",
        update_outcome_fields=True,
    )

    record = list_rule_fire_states(db)["henrique-enters-alone"]
    assert record.last_evaluated_at == 1_700_000_200.0
    assert record.last_outcome == "skipped"
    assert record.last_skip_detail == "Not travelling together: unknown, peer fix is stale"
    assert record.last_skip_reason == "conditions_not_met"

    # A fire-only update that does not opt into outcome fields must not clear them.
    upsert_rule_fire_state(
        db,
        last_error=None,
        last_fired_at=1_700_000_300.0,
        rule_id="henrique-enters-alone",
    )
    record = list_rule_fire_states(db)["henrique-enters-alone"]
    assert record.last_outcome == "skipped"
    assert record.last_skip_reason == "conditions_not_met"

    upsert_rule_fire_state(
        db,
        last_error=None,
        last_evaluated_at=1_700_000_300.0,
        last_fired_at=1_700_000_300.0,
        last_outcome="fired",
        rule_id="henrique-enters-alone",
        update_outcome_fields=True,
    )
    record = list_rule_fire_states(db)["henrique-enters-alone"]
    assert record.last_outcome == "fired"
    assert record.last_skip_detail is None
    assert record.last_skip_reason is None
