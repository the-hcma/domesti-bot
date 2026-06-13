"""Hermetic tests for persisted automation rule fire state."""

from __future__ import annotations

from pathlib import Path

from app.rule_fire_state_store import list_rule_fire_states, upsert_rule_fire_state


def test_upsert_rule_fire_state_round_trips(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    upsert_rule_fire_state(
        db,
        last_error=None,
        last_fired_at=1_700_000_100.0,
        rule_id="arrive-home",
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
