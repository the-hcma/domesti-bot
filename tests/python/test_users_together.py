"""Confidence that two users travel together, from interpolated tracks."""

from __future__ import annotations

import math
import random

import pytest

from app.users_together import (
    InterpolatedPosition,
    TogetherParams,
    TrackFix,
    UnknownReason,
    WindowSpec,
    clean_track,
    estimate_together,
    interpolate_position,
    pair_closeness,
)

_NOW = 100_000.0
_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_LAT0 = 41.0
_LON0 = -73.9


def _fix(
    at: float,
    *,
    accuracy_m: float | None = 5.0,
    lateral_m: float = 0.0,
    speed_mps: float = 20.0,
) -> TrackFix:
    """A fix on a straight road heading north, ``lateral_m`` meters east of the road."""
    lat = _LAT0 + speed_mps * (at - _NOW) / _M_PER_DEG
    lon = _LON0 + lateral_m / (_M_PER_DEG * math.cos(math.radians(_LAT0)))
    return TrackFix(accuracy_m=accuracy_m, at=at, lat=lat, lon=lon)


def _times(*, every_s: float, last_at: float, span_s: float = 700.0) -> list[float]:
    count = int(span_s // every_s)
    return [last_at - index * every_s for index in range(count, -1, -1)]


def _track(times: list[float], **kwargs: float | None) -> list[TrackFix]:
    return [_fix(at, **kwargs) for at in times]  # type: ignore[arg-type]


def _phone_a() -> list[TrackFix]:
    return _track(_times(every_s=10.0, last_at=_NOW), lateral_m=0.0)


@pytest.mark.parametrize("offset_s", [0.0, 3.0, 9.0, 17.0])
def test_two_phones_in_one_car_are_together_whatever_the_report_offset(offset_s: float) -> None:
    phone_b = _track(_times(every_s=19.0, last_at=_NOW - offset_s), lateral_m=3.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    assert estimate.known
    assert estimate.confidence is not None and estimate.confidence >= 0.95
    assert estimate.recent_closeness is not None and estimate.recent_closeness >= 0.95
    assert estimate.is_together(0.8) is True
    assert all(window.known for window in estimate.windows)
    assert estimate.dropped_quality == (0, 0)
    assert estimate.anchor == pytest.approx(_NOW - offset_s)


def test_interpolation_is_what_makes_offset_reports_comparable() -> None:
    phone_b = _track(_times(every_s=19.0, last_at=_NOW - 9.0), lateral_m=3.0)
    latest_a, latest_b = _phone_a()[-1], phone_b[-1]
    raw_gap_m = math.hypot((latest_a.lat - latest_b.lat) * _M_PER_DEG, 3.0)

    assert raw_gap_m > 150.0
    assert estimate_together(_phone_a(), phone_b, now=_NOW).is_together(0.8) is True


def test_two_phones_on_different_roads_are_not_together() -> None:
    phone_b = _track(_times(every_s=19.0, last_at=_NOW - 3.0), lateral_m=400.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    assert estimate.known
    assert estimate.confidence is not None and estimate.confidence < 0.1
    assert estimate.is_together(0.5) is False
    assert estimate.distance_now_m is not None and estimate.distance_now_m > 300.0


def test_one_parked_and_one_moving_are_not_together() -> None:
    parked = _track(_times(every_s=19.0, last_at=_NOW - 3.0), speed_mps=0.0)

    estimate = estimate_together(_phone_a(), parked, now=_NOW)

    assert estimate.known
    assert estimate.is_together(0.5) is False


def test_one_noisy_instant_lowers_the_score_slightly_without_flipping_it() -> None:
    times = _times(every_s=19.0, last_at=_NOW - 3.0)
    phone_b = [_fix(at, lateral_m=3.0) for at in times]
    middle = len(phone_b) // 2
    phone_b[middle] = _fix(times[middle], accuracy_m=10.0, lateral_m=123.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)
    clean = estimate_together(_phone_a(), _track(times, lateral_m=3.0), now=_NOW)

    assert estimate.confidence is not None and clean.confidence is not None
    assert estimate.confidence < clean.confidence
    assert estimate.confidence >= 0.9
    assert estimate.is_together(0.8) is True


def test_a_stale_second_phone_is_unknown_not_apart() -> None:
    phone_b = _track(_times(every_s=19.0, last_at=_NOW - 120.0), lateral_m=3.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    assert not estimate.known
    assert estimate.unknown_reason is UnknownReason.STALE_FIX
    assert estimate.unknown_track == 1
    assert estimate.is_together(0.5) is None
    assert estimate.newest_fix_age_s[1] == pytest.approx(120.0)


def test_a_user_without_any_good_fix_is_unknown() -> None:
    low_quality = _track(_times(every_s=19.0, last_at=_NOW - 3.0), accuracy_m=200.0)

    estimate = estimate_together(_phone_a(), low_quality, now=_NOW)

    assert estimate.unknown_reason is UnknownReason.NO_GOOD_FIXES
    assert estimate.unknown_track == 1
    assert estimate.dropped_quality[1] == len(low_quality)
    assert estimate_together(_phone_a(), [], now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


def test_a_long_gap_in_one_track_in_the_last_minute_is_too_little_coverage() -> None:
    old = _track(_times(every_s=19.0, last_at=_NOW - 100.0), lateral_m=3.0)
    phone_b = [*old, _fix(_NOW - 3.0, lateral_m=3.0)]

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    assert not estimate.known
    assert estimate.unknown_reason is UnknownReason.TOO_LITTLE_COVERAGE
    assert estimate.unknown_track is None
    assert estimate.windows[0].known is False
    assert estimate.windows[0].coverage < 0.5


def test_longer_windows_without_enough_data_are_left_out_not_counted_as_apart() -> None:
    short_a = _track(_times(every_s=10.0, last_at=_NOW, span_s=100.0), lateral_m=0.0)
    short_b = _track(_times(every_s=10.0, last_at=_NOW, span_s=100.0), lateral_m=3.0)

    estimate = estimate_together(short_a, short_b, now=_NOW)

    assert [window.known for window in estimate.windows] == [True, False, False]
    assert estimate.confidence == pytest.approx(estimate.recent_closeness)
    assert estimate.is_together(0.8) is True


def test_a_pickup_a_few_minutes_ago_still_counts_when_the_last_minute_is_close() -> None:
    times_a = _times(every_s=10.0, last_at=_NOW)
    phone_a = _track(times_a, lateral_m=0.0)
    pickup_at = _NOW - 240.0
    phone_b = [_fix(at, lateral_m=3.0 if at >= pickup_at else 400.0) for at in times_a]

    estimate = estimate_together(phone_a, phone_b, now=_NOW)

    assert estimate.recent_closeness is not None and estimate.recent_closeness >= 0.95
    assert estimate.confidence is not None and 0.7 <= estimate.confidence < 0.95
    assert estimate.is_together(0.7) is True
    assert estimate.together_for_s is not None and 200.0 <= estimate.together_for_s <= 260.0


def test_a_long_shared_history_cannot_hide_a_separation_in_the_last_minute() -> None:
    times_a = _times(every_s=10.0, last_at=_NOW)
    phone_a = _track(times_a, lateral_m=0.0)
    phone_b = [_fix(at, lateral_m=3.0 if at < _NOW - 40.0 else 400.0) for at in times_a]

    estimate = estimate_together(phone_a, phone_b, now=_NOW)

    assert estimate.confidence is not None and estimate.recent_closeness is not None
    assert estimate.confidence > estimate.recent_closeness
    assert estimate.recent_closeness < 0.6
    assert estimate.is_together(estimate.confidence - 0.01) is False


def test_the_confidence_is_the_weighted_mean_of_the_window_closeness_values() -> None:
    # Fixes sit exactly on the 10 s grid: near for the last 30 s, far before that.
    params = TogetherParams(windows=(WindowSpec(30.0, 0.5), WindowSpec(60.0, 0.5)))
    times = [_NOW - 10.0 * index for index in range(12, -1, -1)]
    phone_a = [_fix(at, speed_mps=0.0) for at in times]
    phone_b = [_fix(at, speed_mps=0.0, lateral_m=3.0 if at >= _NOW - 30.0 else 400.0) for at in times]

    estimate = estimate_together(phone_a, phone_b, now=_NOW, params=params)

    assert [window.closeness for window in estimate.windows] == [pytest.approx(1.0), pytest.approx(4 / 7)]
    assert estimate.confidence == pytest.approx(0.5 * 1.0 + 0.5 * 4 / 7)
    assert estimate.is_together(0.78) is True
    assert estimate.is_together(0.8) is False


def test_a_steady_separation_gives_the_same_closeness_in_every_window() -> None:
    phone_b = _track(_times(every_s=10.0, last_at=_NOW), lateral_m=100.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    # near = 30 m + 5 m + 5 m, far = 150 m: (150 - 100) / (150 - 40)
    assert estimate.confidence == pytest.approx(50 / 110, abs=0.01)
    assert estimate.distance_now_m == pytest.approx(100.0, abs=0.5)
    assert estimate.is_together(0.8) is False


def test_reported_accuracy_widens_the_near_zone() -> None:
    sloppy_a = _track(_times(every_s=10.0, last_at=_NOW), accuracy_m=40.0)
    sloppy_b = _track(_times(every_s=10.0, last_at=_NOW), accuracy_m=40.0, lateral_m=100.0)

    estimate = estimate_together(sloppy_a, sloppy_b, now=_NOW)

    # near = 30 + 40 + 40 = 110 m, so 100 m apart still counts as together
    assert estimate.confidence == pytest.approx(1.0)


def test_a_low_accuracy_fix_between_two_good_ones_is_skipped_and_bridged() -> None:
    times = _times(every_s=19.0, last_at=_NOW - 3.0)
    phone_b = [_fix(at, lateral_m=3.0) for at in times]
    phone_b[len(phone_b) // 2] = _fix(times[len(phone_b) // 2], accuracy_m=500.0, lateral_m=900.0)

    estimate = estimate_together(_phone_a(), phone_b, now=_NOW)

    assert estimate.dropped_quality[1] == 1
    assert estimate.known
    assert estimate.confidence is not None and estimate.confidence >= 0.95


def test_a_poor_accuracy_fix_would_pull_the_estimate_apart_if_it_were_not_filtered() -> None:
    # 500 m off is plausible as a speed (about 33 m/s), so only the accuracy filter can remove it.
    times = _times(every_s=19.0, last_at=_NOW - 3.0)
    bad_index = len(times) - 2
    reported = [_fix(at, lateral_m=3.0) for at in times]
    poor = [*reported]
    poor[bad_index] = _fix(times[bad_index], accuracy_m=500.0, lateral_m=500.0)
    accurate = [*reported]
    accurate[bad_index] = _fix(times[bad_index], accuracy_m=5.0, lateral_m=500.0)

    filtered = estimate_together(_phone_a(), poor, now=_NOW)
    unfiltered = estimate_together(_phone_a(), accurate, now=_NOW)

    assert filtered.dropped_quality[1] == 1
    assert unfiltered.dropped_quality[1] == 0
    assert filtered.confidence is not None and unfiltered.confidence is not None
    assert filtered.confidence > unfiltered.confidence + 0.1


def test_clean_track_drops_missing_accuracy_poor_accuracy_nonfinite_and_duplicate_times() -> None:
    params = TogetherParams()
    good = _fix(_NOW - 30.0)
    fixes = [
        _fix(_NOW - 10.0),
        good,
        _fix(_NOW - 20.0, accuracy_m=None),
        _fix(_NOW - 25.0, accuracy_m=51.0),
        TrackFix(accuracy_m=5.0, at=_NOW - 15.0, lat=math.nan, lon=_LON0),
        _fix(_NOW - 30.0),
    ]

    cleaned = clean_track(fixes, params=params)

    assert [fix.at for fix in cleaned.fixes] == [_NOW - 30.0, _NOW - 10.0]
    assert cleaned.dropped_quality == 4
    assert cleaned.dropped_speed == 0


def test_clean_track_sorts_oldest_first() -> None:
    shuffled = _track([_NOW - 40.0, _NOW - 10.0, _NOW - 30.0, _NOW - 20.0])
    random.Random(7).shuffle(shuffled)

    assert [fix.at for fix in clean_track(shuffled, params=TogetherParams()).fixes] == [
        _NOW - 40.0,
        _NOW - 30.0,
        _NOW - 20.0,
        _NOW - 10.0,
    ]


def test_clean_track_drops_an_impossible_jump_but_keeps_real_driving() -> None:
    times = [_NOW - 50.0, _NOW - 40.0, _NOW - 30.0, _NOW - 20.0, _NOW - 10.0]
    fixes = _track(times)
    fixes[2] = _fix(times[2], lateral_m=20_000.0)

    cleaned = clean_track(fixes, params=TogetherParams())

    assert [fix.at for fix in cleaned.fixes] == [times[0], times[1], times[3], times[4]]
    assert cleaned.dropped_speed == 1
    assert len(clean_track(_track(times), params=TogetherParams()).fixes) == 5


def test_clean_track_drops_an_end_fix_that_jumps_but_not_the_neighbor_it_jumped_from() -> None:
    times = [_NOW - 30.0, _NOW - 20.0, _NOW - 10.0, _NOW]
    fixes = _track(times)
    fixes[3] = _fix(times[3], lateral_m=20_000.0)

    cleaned = clean_track(fixes, params=TogetherParams())

    assert [fix.at for fix in cleaned.fixes] == times[:3]
    assert cleaned.dropped_speed == 1


def test_interpolate_position_between_exact_and_missing_instants() -> None:
    track = [_fix(_NOW - 20.0), _fix(_NOW - 10.0, accuracy_m=9.0), _fix(_NOW)]

    exact = interpolate_position(track, _NOW - 10.0, max_gap_s=45.0)
    middle = interpolate_position(track, _NOW - 5.0, max_gap_s=45.0)

    assert exact is not None and exact.lat == pytest.approx(track[1].lat)
    assert middle is not None
    assert middle.lat == pytest.approx((track[1].lat + track[2].lat) / 2)
    assert middle.accuracy_m == 9.0
    assert interpolate_position(track, _NOW + 1.0, max_gap_s=45.0) is None
    assert interpolate_position(track, _NOW - 21.0, max_gap_s=45.0) is None
    assert interpolate_position([], _NOW, max_gap_s=45.0) is None


def test_interpolate_position_refuses_to_bridge_a_gap_longer_than_the_limit() -> None:
    track = [_fix(_NOW - 100.0), _fix(_NOW)]

    assert interpolate_position(track, _NOW - 50.0, max_gap_s=45.0) is None
    assert interpolate_position(track, _NOW - 50.0, max_gap_s=120.0) is not None


def test_pair_closeness_is_full_within_the_near_zone_and_zero_past_far() -> None:
    params = TogetherParams(near_m=30.0, far_m=150.0)
    here = InterpolatedPosition(accuracy_m=5.0, lat=_LAT0, lon=_LON0)

    def at(distance_m: float) -> InterpolatedPosition:
        return InterpolatedPosition(accuracy_m=5.0, lat=_LAT0 + distance_m / _M_PER_DEG, lon=_LON0)

    assert pair_closeness(here, at(40.0), params=params)[1] == 1.0
    assert pair_closeness(here, at(160.0), params=params)[1] == 0.0
    assert pair_closeness(here, at(400.0), params=params)[1] == 0.0
    midpoint = pair_closeness(here, at(95.0), params=params)[1]
    assert 0.4 < midpoint < 0.6


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"windows": ()}, "at least one window"),
        ({"step_s": 0.0}, "step_s greater than 0"),
        ({"near_m": 50.0, "far_m": 50.0}, "near_m < far_m"),
        ({"coverage_floor": 1.5}, "coverage_floor in"),
        ({"windows": (WindowSpec(300.0, 0.5), WindowSpec(60.0, 0.5))}, "ascending order"),
        ({"windows": (WindowSpec(60.0, 0.0),)}, "positive window weights"),
    ],
)
def test_parameters_are_validated(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TogetherParams(**kwargs)  # type: ignore[arg-type]
