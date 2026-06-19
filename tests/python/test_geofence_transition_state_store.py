"""Hermetic tests for geofence transition state persistence."""

from __future__ import annotations

from pathlib import Path

from app.geofence_transition_state_store import (
    list_geofence_transition_states,
    upsert_geofence_transition_state,
)


def test_upsert_and_list_geofence_transition_state(tmp_path: Path) -> None:
    """Persisted rows round-trip through the store facade."""
    db = tmp_path / "discovery.sqlite"
    upsert_geofence_transition_state(
        db,
        geofence_id="house",
        inside_since=None,
        last_location_received_at=1_700_000_000.0,
        outside_since=1_700_000_000.0,
        user_id="henrique",
        was_inside=False,
    )
    states = list_geofence_transition_states(db)
    record = states[("henrique", "house")]
    assert record.user_id == "henrique"
    assert record.geofence_id == "house"
    assert record.was_inside is False
    assert record.outside_since == 1_700_000_000.0
    assert record.inside_since is None
    assert record.last_location_received_at == 1_700_000_000.0

    upsert_geofence_transition_state(
        db,
        geofence_id="house",
        inside_since=1_700_000_100.0,
        last_location_received_at=1_700_000_100.0,
        outside_since=None,
        user_id="henrique",
        was_inside=True,
    )
    updated = list_geofence_transition_states(db)[("henrique", "house")]
    assert updated.was_inside is True
    assert updated.inside_since == 1_700_000_100.0
    assert updated.outside_since is None
