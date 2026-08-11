"""Hermetic tests for overnight EP1 false-positive calibration helpers."""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest

from app.device_enums import Ep1OccupancyTuningKind
from app.ep1_calibration import Ep1SettingsTarget
from app.ep1_occupancy_tuning import Ep1OccupancyTuningField, Ep1OccupancyTuningSnapshot
from app.ep1_overnight_calibration import (
    DEFAULT_MAX_CONSECUTIVE_OBSERVE_FAILURES,
    DEFAULT_OBSERVE_RETRY_COUNT,
    EP1_OVERNIGHT_CALIBRATION_OBSERVE_FAILURES_EXHAUSTED,
    Ep1OvernightCalibrationError,
    KnobAdjustDirection,
    KnobAdjustment,
    OccupancyObservation,
    OvernightCalibrationCycleResult,
    _run_one_cycle,
    in_empty_room_window,
    propose_next_false_positive_adjustment,
    run_overnight_ep1_calibration,
    seconds_until_empty_room_window,
    seconds_until_empty_room_window_end,
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


def test_seconds_until_empty_room_window_end_inside() -> None:
    tz = ZoneInfo("UTC")
    remaining = seconds_until_empty_room_window_end(datetime(2026, 8, 11, 5, 0, tzinfo=tz))
    assert remaining == pytest.approx(3600.0)


def test_seconds_until_empty_room_window_end_outside_is_zero() -> None:
    tz = ZoneInfo("UTC")
    assert seconds_until_empty_room_window_end(datetime(2026, 8, 11, 12, 0, tzinfo=tz)) == 0.0


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


def test_propose_decreases_misaligned_max_distance() -> None:
    snap = _snapshot(
        {
            Ep1OccupancyTuningKind.MAX_DISTANCE: _field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=4.04,
                min_value=0.0,
                max_value=8.0,
                step=0.1,
            ),
        }
    )
    adj = propose_next_false_positive_adjustment(snap, attempt_index=0)
    assert adj is not None
    assert adj.kind == Ep1OccupancyTuningKind.MAX_DISTANCE
    assert adj.new_value == pytest.approx(3.9)


@pytest.mark.asyncio
async def test_run_does_not_advance_attempt_index_when_write_unconfirmed(
    tmp_path: Path,
) -> None:
    """Unconfirmed knob writes must retry the same lever, not rotate past it."""

    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    adjustment = KnobAdjustment(
        direction=KnobAdjustDirection.DECREASE,
        kind=Ep1OccupancyTuningKind.MAX_DISTANCE,
        new_value=4.9,
        old_value=5.0,
        step=0.1,
    )
    fp_obs = OccupancyObservation(
        duration_s=1.0,
        false_positive=True,
        final_occupied=True,
        occupied_sample_count=1,
        sample_count=2,
    )
    clear_obs = OccupancyObservation(
        duration_s=1.0,
        false_positive=False,
        final_occupied=False,
        occupied_sample_count=0,
        sample_count=1,
    )
    attempt_indices: list[int] = []

    async def _fake_cycle(**kwargs: object) -> OvernightCalibrationCycleResult:
        attempt_index = kwargs["attempt_index"]
        assert isinstance(attempt_index, int)
        attempt_indices.append(attempt_index)
        call_n = len(attempt_indices)
        if call_n <= 2:
            return OvernightCalibrationCycleResult(
                adjustment=adjustment,
                applied=False,
                clear_streak=0,
                dry_run=False,
                observation=fp_obs,
                knobs={},
            )
        if call_n == 3:
            return OvernightCalibrationCycleResult(
                adjustment=adjustment,
                applied=True,
                clear_streak=0,
                dry_run=False,
                observation=fp_obs,
                knobs={},
            )
        return OvernightCalibrationCycleResult(
            adjustment=None,
            applied=False,
            clear_streak=1,
            dry_run=False,
            observation=clear_obs,
            knobs={},
        )

    with (
        patch(
            "app.ep1_overnight_calibration.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_overnight_calibration._run_one_cycle",
            new=AsyncMock(side_effect=_fake_cycle),
        ),
        patch("app.ep1_overnight_calibration.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await run_overnight_ep1_calibration(
            device_id=target.device_id,
            clear_streak_required=1,
            force_window=True,
            log_path=tmp_path / "calibrate.jsonl",
            observe_s=1.0,
            settle_s=0.0,
            timezone_name="UTC",
        )

    assert attempt_indices[:4] == [0, 0, 0, 1]
    assert result.false_positives == 3
    assert result.success is True


@pytest.mark.asyncio
async def test_run_continues_after_inconclusive_observe(tmp_path: Path) -> None:
    """Transient observe failure must not abort the overnight run."""

    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    clear_obs = OccupancyObservation(
        duration_s=1.0,
        false_positive=False,
        final_occupied=False,
        occupied_sample_count=0,
        sample_count=1,
    )
    placeholder = OccupancyObservation(
        duration_s=1.0,
        false_positive=False,
        final_occupied=None,
        occupied_sample_count=0,
        sample_count=0,
    )
    calls = 0

    async def _fake_cycle(**_kwargs: object) -> OvernightCalibrationCycleResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            return OvernightCalibrationCycleResult(
                adjustment=None,
                applied=False,
                clear_streak=0,
                dry_run=False,
                observation=placeholder,
                knobs={},
                inconclusive=True,
            )
        return OvernightCalibrationCycleResult(
            adjustment=None,
            applied=False,
            clear_streak=1,
            dry_run=False,
            observation=clear_obs,
            knobs={},
        )

    with (
        patch(
            "app.ep1_overnight_calibration.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_overnight_calibration._run_one_cycle",
            new=AsyncMock(side_effect=_fake_cycle),
        ),
    ):
        result = await run_overnight_ep1_calibration(
            device_id=target.device_id,
            clear_streak_required=1,
            force_window=True,
            log_path=tmp_path / "calibrate.jsonl",
            observe_s=1.0,
            settle_s=0.0,
            timezone_name="UTC",
        )

    assert calls == 2
    assert result.cycles == 1
    assert result.success is True


@pytest.mark.asyncio
async def test_run_ends_cleanly_when_remaining_observe_too_short(tmp_path: Path) -> None:
    """Tiny remaining window must exit as window_ended, not abort on empty samples."""

    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    mock_cycle = AsyncMock()
    with (
        patch(
            "app.ep1_overnight_calibration.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_overnight_calibration.seconds_until_empty_room_window_end",
            return_value=1.0,
        ),
        patch(
            "app.ep1_overnight_calibration._run_one_cycle",
            new=mock_cycle,
        ),
    ):
        result = await run_overnight_ep1_calibration(
            device_id=target.device_id,
            clear_streak_required=1,
            force_window=False,
            log_path=tmp_path / "calibrate.jsonl",
            observe_s=90.0,
            settle_s=0.0,
            timezone_name="UTC",
            window_end_hour=0,
            window_start_hour=0,
        )

    assert result.window_ended is True
    assert result.success is False
    mock_cycle.assert_not_called()


@pytest.mark.asyncio
async def test_run_one_cycle_retries_observe_then_marks_inconclusive(
    tmp_path: Path,
) -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    observe = AsyncMock(side_effect=Ep1OvernightCalibrationError("WiFi blip"))
    with (
        patch("app.ep1_overnight_calibration.observe_ep1_occupancy", new=observe),
        patch("app.ep1_overnight_calibration.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await _run_one_cycle(
            target=target,
            attempt_index=0,
            cache_path=None,
            clear_streak=2,
            dry_run=False,
            ep1_mgr=None,
            force_window=True,
            log_path=tmp_path / "calibrate.jsonl",
            noise_psk=None,
            observe_s=1.0,
            timezone=ZoneInfo("UTC"),
            window_end_hour=6,
            window_start_hour=0,
        )

    assert result.inconclusive is True
    assert result.clear_streak == 2
    assert observe.await_count == DEFAULT_OBSERVE_RETRY_COUNT


@pytest.mark.asyncio
async def test_run_aborts_after_consecutive_inconclusive_observes(tmp_path: Path) -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    placeholder = OccupancyObservation(
        duration_s=1.0,
        false_positive=False,
        final_occupied=None,
        occupied_sample_count=0,
        sample_count=0,
    )

    async def _always_inconclusive(**_kwargs: object) -> OvernightCalibrationCycleResult:
        return OvernightCalibrationCycleResult(
            adjustment=None,
            applied=False,
            clear_streak=0,
            dry_run=False,
            observation=placeholder,
            knobs={},
            inconclusive=True,
        )

    with (
        patch(
            "app.ep1_overnight_calibration.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_overnight_calibration._run_one_cycle",
            new=AsyncMock(side_effect=_always_inconclusive),
        ),
        patch(
            "app.ep1_overnight_calibration.DEFAULT_MAX_CONSECUTIVE_OBSERVE_FAILURES",
            2,
        ),
    ):
        with pytest.raises(Ep1OvernightCalibrationError) as raised:
            await run_overnight_ep1_calibration(
                device_id=target.device_id,
                clear_streak_required=1,
                force_window=True,
                log_path=tmp_path / "calibrate.jsonl",
                observe_s=1.0,
                settle_s=0.0,
                timezone_name="UTC",
            )

    assert EP1_OVERNIGHT_CALIBRATION_OBSERVE_FAILURES_EXHAUSTED.format(
        count=2,
        device_id=target.device_id,
    ) in str(raised.value)
    assert DEFAULT_MAX_CONSECUTIVE_OBSERVE_FAILURES >= 2


@pytest.mark.asyncio
async def test_run_stops_cleanly_when_stop_event_set(tmp_path: Path) -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:ff",
        display_label="EP1 (aa:bb:cc:dd:ee:ff)",
        display_name="EP1",
        host="192.0.2.10",
        port=6053,
    )
    clear_obs = OccupancyObservation(
        duration_s=1.0,
        false_positive=False,
        final_occupied=False,
        occupied_sample_count=0,
        sample_count=1,
    )
    stop_event = asyncio.Event()
    calls = 0

    async def _fake_cycle(**_kwargs: object) -> OvernightCalibrationCycleResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            stop_event.set()
            return OvernightCalibrationCycleResult(
                adjustment=None,
                applied=False,
                clear_streak=1,
                dry_run=False,
                observation=clear_obs,
                knobs={},
            )
        raise AssertionError("should have stopped after stop_event was set")

    with (
        patch(
            "app.ep1_overnight_calibration.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_overnight_calibration._run_one_cycle",
            new=AsyncMock(side_effect=_fake_cycle),
        ),
    ):
        result = await run_overnight_ep1_calibration(
            device_id=target.device_id,
            clear_streak_required=100,
            force_window=True,
            log_path=tmp_path / "calibrate.jsonl",
            observe_s=1.0,
            settle_s=0.0,
            stop_event=stop_event,
            timezone_name="UTC",
        )

    assert result.interrupted is True
    assert result.success is False
    assert calls == 1
