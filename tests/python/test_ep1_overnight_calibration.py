"""Hermetic tests for overnight EP1 false-positive calibration helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.device_enums import Ep1OccupancyTuningKind
from app.ep1_occupancy_tuning import Ep1OccupancyTuningField, Ep1OccupancyTuningSnapshot
from app.ep1_overnight_calibration import (
    Ep1OvernightCalibrationError,
    KnobAdjustDirection,
    in_empty_room_window,
    propose_next_false_positive_adjustment,
    seconds_until_empty_room_window,
)


def _field(
    kind: Ep1OccupancyTuningKind,
    *,
    value: float,
    min_value: float = 0.0,
    max_value: float = 25.0,
    step: float = 0.1,
) -> Ep1OccupancyTuningField:
    return Ep1OccupancyTuningField(
        available=True,
        kind=kind,
        max_value=max_value,
        min_value=min_value,
        step=step,
        unit=None,
        value=value,
    )


def _snapshot(knobs: dict[Ep1OccupancyTuningKind, Ep1OccupancyTuningField]) -> Ep1OccupancyTuningSnapshot:
    full = {
        kind: Ep1OccupancyTuningField(
            available=False,
            kind=kind,
            max_value=None,
            min_value=None,
            step=None,
            unit=None,
            value=None,
        )
        for kind in Ep1OccupancyTuningKind
    }
    full.update(knobs)
    return Ep1OccupancyTuningSnapshot(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        knobs=full,
        port=6053,
    )


def test_in_empty_room_window_equal_hours_is_always_open() -> None:
    tz = ZoneInfo("UTC")
    assert in_empty_room_window(
        datetime(2026, 8, 11, 12, 0, tzinfo=tz),
        start_hour=3,
        end_hour=3,
    )


def test_in_empty_room_window_midnight_to_six() -> None:
    tz = ZoneInfo("America/New_York")
    assert in_empty_room_window(datetime(2026, 8, 11, 0, 0, tzinfo=tz))
    assert in_empty_room_window(datetime(2026, 8, 11, 5, 59, tzinfo=tz))
    assert not in_empty_room_window(datetime(2026, 8, 11, 6, 0, tzinfo=tz))
    assert not in_empty_room_window(datetime(2026, 8, 11, 12, 0, tzinfo=tz))


@pytest.mark.parametrize("bad_hour", [-1, 24])
def test_in_empty_room_window_rejects_out_of_range_hours(bad_hour: int) -> None:
    tz = ZoneInfo("UTC")
    with pytest.raises(Ep1OvernightCalibrationError):
        in_empty_room_window(datetime(2026, 8, 11, 1, 0, tzinfo=tz), start_hour=bad_hour)


def test_in_empty_room_window_wraps_midnight() -> None:
    tz = ZoneInfo("UTC")
    assert in_empty_room_window(
        datetime(2026, 8, 11, 23, 0, tzinfo=tz),
        start_hour=22,
        end_hour=6,
    )
    assert in_empty_room_window(
        datetime(2026, 8, 11, 1, 0, tzinfo=tz),
        start_hour=22,
        end_hour=6,
    )
    assert not in_empty_room_window(
        datetime(2026, 8, 11, 12, 0, tzinfo=tz),
        start_hour=22,
        end_hour=6,
    )


def test_seconds_until_empty_room_window_is_zero_inside() -> None:
    tz = ZoneInfo("UTC")
    assert seconds_until_empty_room_window(datetime(2026, 8, 11, 1, 0, tzinfo=tz)) == 0.0


def test_seconds_until_empty_room_window_counts_to_midnight() -> None:
    tz = ZoneInfo("UTC")
    wait_s = seconds_until_empty_room_window(datetime(2026, 8, 11, 12, 0, tzinfo=tz))
    assert wait_s == pytest.approx(12 * 3600.0)


def test_propose_prefers_lowering_max_distance_first() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=4.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=5.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
        }
    )
    adj = propose_next_false_positive_adjustment(snap, attempt_index=0)
    assert adj is not None
    assert adj.kind == Ep1OccupancyTuningKind.MAX_DISTANCE
    assert adj.direction == KnobAdjustDirection.DECREASE
    assert adj.old_value == pytest.approx(4.0)
    assert adj.new_value == pytest.approx(3.9)


def test_propose_skips_max_distance_at_floor_and_lowers_trigger_sensitivity() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=0.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=5.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
        }
    )
    adj = propose_next_false_positive_adjustment(snap, attempt_index=0)
    assert adj is not None
    assert adj.kind == Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY
    assert adj.new_value == pytest.approx(4.0)


def test_propose_raises_on_latency_when_sensitivities_exhausted() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=0.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=0.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
            Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
                value=0.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
            Ep1OccupancyTuningKind.ON_LATENCY: _field(
                Ep1OccupancyTuningKind.ON_LATENCY,
                value=1.0,
                min_value=0.0,
                max_value=60.0,
                step=0.25,
            ),
        }
    )
    adj = propose_next_false_positive_adjustment(snap, attempt_index=0)
    assert adj is not None
    assert adj.kind == Ep1OccupancyTuningKind.ON_LATENCY
    assert adj.direction == KnobAdjustDirection.INCREASE
    assert adj.new_value == pytest.approx(1.25)


def test_propose_returns_none_when_all_levers_exhausted() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=0.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=0.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
            Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
                value=0.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
            Ep1OccupancyTuningKind.ON_LATENCY: _field(
                Ep1OccupancyTuningKind.ON_LATENCY,
                value=60.0,
                min_value=0.0,
                max_value=60.0,
                step=0.25,
            ),
            Ep1OccupancyTuningKind.TRIGGER_DISTANCE: _field(
                Ep1OccupancyTuningKind.TRIGGER_DISTANCE,
                value=0.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.MIN_DISTANCE: _field(
                Ep1OccupancyTuningKind.MIN_DISTANCE,
                value=8.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
        }
    )
    assert propose_next_false_positive_adjustment(snap, attempt_index=0) is None


def test_propose_rotates_start_lever_with_attempt_index() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=4.0,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=5.0,
                min_value=0.0,
                max_value=9.0,
                step=1.0,
            ),
        }
    )
    adj = propose_next_false_positive_adjustment(snap, attempt_index=1)
    assert adj is not None
    assert adj.kind == Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY
