"""Confidence that two users travel together, from interpolated tracks."""

from __future__ import annotations

import math
import random
from dataclasses import replace
from typing import Any

import pytest

from app.users_together import (
    ApartBasis,
    InterpolatedPosition,
    TogetherEstimate,
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


def _track(
    times: list[float],
    *,
    accuracy_m: float | None = 5.0,
    lateral_m: float = 0.0,
    speed_mps: float = 20.0,
) -> list[TrackFix]:
    return [_fix(at, accuracy_m=accuracy_m, lateral_m=lateral_m, speed_mps=speed_mps) for at in times]


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


def _coarse_fix(at: float, *, north_m: float, accuracy_m: float | None = 100.0) -> TrackFix:
    """A fix ``north_m`` meters north of the start of the road, regardless of the road's own motion."""
    return TrackFix(accuracy_m=accuracy_m, at=at, lat=_LAT0 + north_m / _M_PER_DEG, lon=_LON0)


def test_a_coarse_fix_hundreds_of_kilometers_away_is_apart_not_unknown() -> None:
    far = [_coarse_fix(_NOW - 900.0 + 300.0 * index, north_m=400_000.0) for index in range(4)]

    estimate = estimate_together(_phone_a(), far, now=_NOW)

    assert estimate.known
    assert estimate.unknown_reason is None
    assert estimate.is_apart(0.8) is True
    assert estimate.is_together(0.5) is False
    assert estimate.is_recently_apart(0.8) is True
    assert estimate.distance_now_m == pytest.approx(400_000.0, rel=0.01)
    assert estimate.windows == ()
    assert estimate.apart_basis is ApartBasis.COARSE_FIXES
    assert estimate.dropped_quality[1] == len(far)


def test_a_coarse_fix_near_the_other_user_stays_unknown() -> None:
    near = [_coarse_fix(_NOW - 900.0, north_m=80.0)]

    estimate = estimate_together(_phone_a(), near, now=_NOW)

    assert estimate.unknown_reason is UnknownReason.NO_GOOD_FIXES
    assert estimate.unknown_track == 1
    assert estimate.is_apart(0.8) is None


def test_a_coarse_fix_whose_circle_reaches_the_other_user_stays_unknown() -> None:
    far = [_coarse_fix(_NOW - 5.0, north_m=300.0, accuracy_m=200.0)]

    assert estimate_together(_phone_a(), far, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


def test_an_old_coarse_fix_could_have_driven_to_the_other_user_so_it_stays_unknown() -> None:
    maximum_reach_m = TogetherParams().max_speed_mps * 900.0
    reachable = [_coarse_fix(_NOW - 900.0, north_m=0.5 * maximum_reach_m)]
    unreachable = [_coarse_fix(_NOW - 900.0, north_m=2.0 * maximum_reach_m)]

    assert estimate_together(_phone_a(), reachable, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES
    assert estimate_together(_phone_a(), unreachable, now=_NOW).is_apart(0.8) is True


def test_a_coarse_fix_without_a_reported_accuracy_is_not_trusted() -> None:
    far = [_coarse_fix(_NOW - 60.0, north_m=400_000.0, accuracy_m=None)]

    assert estimate_together(_phone_a(), far, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


def test_two_coarse_tracks_far_from_each_other_are_apart() -> None:
    first = [_coarse_fix(_NOW - 60.0, north_m=0.0)]
    second = [_coarse_fix(_NOW - 120.0, north_m=400_000.0)]

    estimate = estimate_together(first, second, now=_NOW)

    assert estimate.is_apart(0.8) is True
    assert estimate.newest_fix_age_s == (pytest.approx(60.0), pytest.approx(120.0))


def test_the_newest_coarse_fix_decides_not_an_older_far_one() -> None:
    fixes = [_coarse_fix(_NOW - 900.0, north_m=400_000.0), _coarse_fix(_NOW - 30.0, north_m=50.0)]

    assert estimate_together(_phone_a(), fixes, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


def test_a_newer_coarse_fix_near_the_other_user_beats_an_older_good_fix_far_away() -> None:
    first = [_coarse_fix(_NOW - 1.0, north_m=0.0)]
    old_good = TrackFix(accuracy_m=5.0, at=_NOW - 10.0, lat=_LAT0 + 1_000.0 / _M_PER_DEG, lon=_LON0)
    second = [old_good, _coarse_fix(_NOW - 1.0, north_m=20.0)]

    assert estimate_together(first, second, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES
    assert estimate_together(first, [old_good], now=_NOW).is_apart(0.8) is True


def test_equal_coarse_timestamps_prefer_the_more_accurate_fix_whatever_the_order() -> None:
    far = _coarse_fix(_NOW - 5.0, north_m=400_000.0, accuracy_m=100.0)
    near = _coarse_fix(_NOW - 5.0, north_m=10.0, accuracy_m=60.0)

    for fixes in ([far, near], [near, far]):
        assert estimate_together(_phone_a(), fixes, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


def _repeated_fix(*, north_m: float, fix_age_s: float, report_ages_s: list[float]) -> list[TrackFix]:
    """The same cached fix re-sent by one report per entry of ``report_ages_s`` (seconds before now)."""
    return [
        TrackFix(
            accuracy_m=19.0,
            at=_NOW - fix_age_s,
            lat=_LAT0 + north_m / _M_PER_DEG,
            lon=_LON0,
            reported_at=_NOW - age,
        )
        for age in report_ages_s
    ]


def test_report_only_fixes_do_not_change_the_estimate() -> None:
    extra = TrackFix(accuracy_m=10.0, at=_NOW - 2_000.0, lat=_LAT0 + 0.5, lon=_LON0, report_only=True)

    with_extra = estimate_together(_phone_a(), [*_phone_a(), extra], now=_NOW)
    without = estimate_together(_phone_a(), _phone_a(), now=_NOW)

    assert with_extra == without


def test_a_report_only_coarse_fix_is_not_the_current_position() -> None:
    live = _coarse_fix(_NOW - 30.0, north_m=80.0)
    kept = TrackFix(
        accuracy_m=100.0,
        at=_NOW - 5.0,
        lat=_LAT0 + 0.5,
        lon=_LON0,
        report_only=True,
    )

    estimate = estimate_together(_phone_a(), [live, kept], now=_NOW)

    assert not estimate.known
    assert estimate.apart_basis is None


def test_a_peer_that_keeps_re_reporting_an_old_fix_far_away_is_apart() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0, 4_600.0])

    estimate = estimate_together(_phone_a(), peer, now=_NOW)

    assert estimate.known
    assert estimate.unknown_reason is None
    assert estimate.apart_basis is ApartBasis.STATIONARY_PEER
    assert estimate.is_apart(0.8) is True
    assert estimate.is_together(0.5) is False
    assert estimate.distance_now_m == pytest.approx(190_000.0, rel=0.01)
    assert estimate.windows == ()


def test_a_peer_re_reporting_an_old_fix_in_either_position_of_the_pair_is_apart() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])

    assert estimate_together(peer, _phone_a(), now=_NOW).is_apart(0.8) is True


def test_a_single_report_of_an_old_fix_is_still_stale() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0])

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_one_report_delivered_twice_is_a_single_observation() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 950.0])

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_re_reports_that_are_not_recent_leave_the_peer_stale() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[1_500.0, 2_750.0])

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_re_reports_older_than_an_hour_do_not_count() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 3_700.0])

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_reports_with_different_fixes_are_not_a_stationary_peer() -> None:
    peer = [
        TrackFix(accuracy_m=19.0, at=_NOW - 11_000.0, lat=_LAT0 + 1.7, lon=_LON0, reported_at=_NOW - 950.0),
        TrackFix(accuracy_m=19.0, at=_NOW - 12_000.0, lat=_LAT0 + 1.7, lon=_LON0, reported_at=_NOW - 2_750.0),
    ]

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_stationary_peer_near_the_other_user_stays_unknown() -> None:
    peer = _repeated_fix(north_m=100.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])

    assert estimate_together(_phone_a(), peer, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_a_newer_coarse_fix_means_the_peer_has_moved() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer.append(
        TrackFix(accuracy_m=400.0, at=_NOW - 900.0, lat=_LAT0 + 1.7, lon=_LON0 + 0.01, reported_at=_NOW - 900.0),
    )

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_coarse_report_at_the_same_fix_time_but_another_position_means_the_peer_has_moved() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer.append(
        TrackFix(accuracy_m=400.0, at=_NOW - 11_000.0, lat=_LAT0 + 1.9, lon=_LON0, reported_at=_NOW - 900.0),
    )

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_later_report_at_another_position_with_an_earlier_fix_time_means_the_peer_has_moved() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer.append(
        TrackFix(accuracy_m=400.0, at=_NOW - 20_000.0, lat=_LAT0 + 0.5, lon=_LON0, reported_at=_NOW - 100.0),
    )

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_report_at_another_position_before_the_repeated_ones_does_not_cancel_them() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer.append(
        TrackFix(accuracy_m=400.0, at=_NOW - 20_000.0, lat=_LAT0 + 0.5, lon=_LON0, reported_at=_NOW - 5_000.0),
    )

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is ApartBasis.STATIONARY_PEER


def test_the_largest_accuracy_among_the_matching_reports_sets_the_uncertainty() -> None:
    peer = _repeated_fix(north_m=1_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer[1] = replace(peer[1], accuracy_m=900.0)

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_matching_report_without_an_accuracy_is_not_stationary_evidence() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    peer[1] = replace(peer[1], accuracy_m=None)

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_newer_coarse_fix_of_the_other_user_near_the_stationary_peer_keeps_it_unknown() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    fresh_good = _track(_times(every_s=10.0, last_at=_NOW - 10.0))
    near_peer = _coarse_fix(_NOW - 5.0, north_m=190_000.0)

    assert estimate_together(fresh_good, peer, now=_NOW).apart_basis is ApartBasis.STATIONARY_PEER
    assert estimate_together([*fresh_good, near_peer], peer, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_a_fix_without_report_times_is_never_stationary_evidence() -> None:
    peer = [
        replace(fix, reported_at=None)
        for fix in _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    ]

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_the_other_user_must_be_fresh_for_a_stationary_peer_to_settle_the_pair() -> None:
    peer = _repeated_fix(north_m=190_000.0, fix_age_s=11_000.0, report_ages_s=[950.0, 2_750.0])
    stale_other = _track(_times(every_s=10.0, last_at=_NOW - 600.0))

    assert estimate_together(peer, stale_other, now=_NOW).apart_basis is not ApartBasis.STATIONARY_PEER


def test_a_coarse_fix_with_nonsense_coordinates_is_ignored() -> None:
    bad = [TrackFix(accuracy_m=100.0, at=_NOW - 60.0, lat=float("nan"), lon=_LON0)]

    assert estimate_together(_phone_a(), bad, now=_NOW).unknown_reason is UnknownReason.NO_GOOD_FIXES


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

    # near = 30 + 5 + 5 = 40 m and far = 150 + 5 + 5 = 160 m: (160 - 100) / (160 - 40)
    assert estimate.confidence == pytest.approx(0.5, abs=0.01)
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
    assert pair_closeness(here, at(180.0), params=params)[1] == 0.0
    assert pair_closeness(here, at(400.0), params=params)[1] == 0.0
    midpoint = pair_closeness(here, at(95.0), params=params)[1]
    assert 0.4 < midpoint < 0.6


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"windows": ()}, "at least one window"),
        ({"step_s": 0.0}, "step_s to be a finite number"),
        ({"near_m": 50.0, "far_m": 50.0}, "near_m < far_m"),
        ({"coverage_floor": 1.5}, "coverage_floor in"),
        ({"windows": (WindowSpec(300.0, 0.5), WindowSpec(60.0, 0.5))}, "ascending order"),
        ({"windows": (WindowSpec(60.0, 0.0),)}, "positive window weights"),
        ({"windows": (WindowSpec(60.0, math.nan),)}, "positive window weights"),
        ({"windows": (WindowSpec(65.0, 1.0),)}, "multiples of step_s"),
        ({"windows": (WindowSpec(0.0, 1.0),)}, "positive window lengths"),
        ({"step_s": math.nan}, "step_s to be a finite number"),
        ({"near_m": -1.0}, "near_m < far_m"),
        ({"max_gap_s": -5.0}, "max_gap_s to be a finite number"),
    ],
)
def test_parameters_are_validated(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        TogetherParams(**kwargs)


def test_together_for_s_does_not_count_missing_history_as_togetherness() -> None:
    short_a = _track(_times(every_s=10.0, last_at=_NOW, span_s=70.0))
    short_b = _track(_times(every_s=10.0, last_at=_NOW, span_s=70.0), lateral_m=3.0)

    estimate = estimate_together(short_a, short_b, now=_NOW)

    assert [window.known for window in estimate.windows] == [True, False, False]
    assert estimate.together_for_s is not None and estimate.together_for_s <= 70.0


def test_together_for_s_stops_at_a_hole_in_the_data() -> None:
    recent = _times(every_s=10.0, last_at=_NOW, span_s=30.0)
    old = _times(every_s=10.0, last_at=_NOW - 400.0, span_s=300.0)
    phone_a = _track([*old, *recent])
    phone_b = _track([*old, *recent], lateral_m=3.0)

    estimate = estimate_together(phone_a, phone_b, now=_NOW)

    assert estimate.together_for_s is not None and estimate.together_for_s <= 30.0


def test_together_for_s_is_zero_when_apart_now() -> None:
    phone_b = _track(_times(every_s=10.0, last_at=_NOW), lateral_m=400.0)

    assert estimate_together(_phone_a(), phone_b, now=_NOW).together_for_s == 0.0


def test_a_nan_timestamp_cannot_disturb_the_order_of_the_other_fixes() -> None:
    times = [_NOW - 10.0 * index for index in range(10, 0, -1)]
    fixes = [*_track(times), TrackFix(accuracy_m=5.0, at=math.nan, lat=_LAT0, lon=_LON0)]
    random.Random(11).shuffle(fixes)

    cleaned = clean_track(fixes, params=TogetherParams())

    assert [fix.at for fix in cleaned.fixes] == times
    assert cleaned.dropped_quality == 1


def test_clean_track_keeps_the_more_accurate_of_two_fixes_at_the_same_time() -> None:
    sloppy = _fix(_NOW - 10.0, accuracy_m=40.0)
    precise = _fix(_NOW - 10.0, accuracy_m=4.0)

    cleaned = clean_track([sloppy, precise, _fix(_NOW)], params=TogetherParams())

    assert [fix.accuracy_m for fix in cleaned.fixes] == [4.0, 5.0]
    assert cleaned.dropped_quality == 1


def test_clean_track_drops_out_of_range_coordinates() -> None:
    off_the_map = TrackFix(accuracy_m=5.0, at=_NOW - 10.0, lat=200.0, lon=_LON0)

    cleaned = clean_track([off_the_map, _fix(_NOW)], params=TogetherParams())

    assert len(cleaned.fixes) == 1
    assert cleaned.dropped_quality == 1


def test_two_fixes_a_millisecond_apart_are_not_a_spike_even_when_exact() -> None:
    # Zero accuracy removes the accuracy discount, so only the one-second floor on the time gap protects this.
    lead = _track([_NOW - 30.0, _NOW - 20.0, _NOW - 10.0], accuracy_m=0.0)
    last = TrackFix(accuracy_m=0.0, at=_NOW - 0.001, lat=lead[-1].lat + 100.0 / _M_PER_DEG, lon=_LON0)
    newest = TrackFix(accuracy_m=0.0, at=_NOW, lat=last.lat + 3.0 / _M_PER_DEG, lon=_LON0)

    assert len(clean_track([*lead, last, newest], params=TogetherParams()).fixes) == 5


def test_a_jump_inside_the_two_accuracy_circles_is_not_a_spike() -> None:
    # 140 m in one second is 140 m/s, but two fixes with 50 m accuracy explain up to 100 m of it (40 m/s).
    lead = _track([_NOW - 30.0, _NOW - 20.0, _NOW - 10.0], accuracy_m=50.0)
    before = TrackFix(accuracy_m=50.0, at=_NOW - 1.0, lat=lead[-1].lat, lon=_LON0)
    newest = TrackFix(accuracy_m=50.0, at=_NOW, lat=before.lat + 140.0 / _M_PER_DEG, lon=_LON0)

    assert len(clean_track([*lead, before, newest], params=TogetherParams()).fixes) == 5
    unexplained = TrackFix(accuracy_m=1.0, at=_NOW, lat=before.lat + 140.0 / _M_PER_DEG, lon=_LON0)
    assert len(clean_track([*lead, before, unexplained], params=TogetherParams()).fixes) == 4


def test_a_cluster_of_two_consecutive_bad_fixes_is_a_known_limitation() -> None:
    # Documented in clean_track: only a single spike is detected; the accuracy filter is the main protection.
    good = _track([_NOW - 60.0 + 10.0 * index for index in range(5)])
    far_a = TrackFix(accuracy_m=5.0, at=_NOW - 5.0, lat=_LAT0 + 1.0, lon=_LON0)
    far_b = TrackFix(accuracy_m=5.0, at=_NOW, lat=_LAT0 + 1.0, lon=_LON0)

    cleaned = clean_track([*good, far_a, far_b], params=TogetherParams())

    assert cleaned.dropped_speed == 0
    assert len(cleaned.fixes) == 7


def test_interpolation_does_not_go_the_long_way_round_the_antimeridian() -> None:
    track = [
        TrackFix(accuracy_m=5.0, at=_NOW - 10.0, lat=0.0, lon=179.9995),
        TrackFix(accuracy_m=5.0, at=_NOW, lat=0.0, lon=-179.9995),
    ]

    middle = interpolate_position(track, _NOW - 5.0, max_gap_s=45.0)

    assert middle is not None and abs(middle.lon) == pytest.approx(180.0, abs=0.001)


def test_the_anchor_lag_is_exposed_when_one_phone_reported_a_while_ago() -> None:
    fresh = estimate_together(_phone_a(), _track(_times(every_s=10.0, last_at=_NOW), lateral_m=3.0), now=_NOW)
    lagging_b = _track(_times(every_s=10.0, last_at=_NOW - 40.0), lateral_m=3.0)
    lagging = estimate_together(_phone_a(), lagging_b, now=_NOW)

    assert fresh.anchor_lag_s == 0.0
    assert lagging.known
    assert lagging.anchor_lag_s == pytest.approx(40.0)
    assert lagging.anchor == pytest.approx(_NOW - 40.0)


def test_freshness_is_inclusive_at_the_limit_and_stale_just_past_it() -> None:
    at_limit = _track(_times(every_s=10.0, last_at=_NOW - 45.0), lateral_m=3.0)
    past_limit = _track(_times(every_s=10.0, last_at=_NOW - 46.0), lateral_m=3.0)

    assert estimate_together(_phone_a(), at_limit, now=_NOW).known
    assert estimate_together(_phone_a(), past_limit, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_the_window_instant_counts_are_pinned() -> None:
    estimate = estimate_together(_phone_a(), _track(_times(every_s=10.0, last_at=_NOW), lateral_m=3.0), now=_NOW)

    assert [(window.total, window.usable) for window in estimate.windows] == [(7, 7), (31, 31), (61, 61)]


def test_coverage_is_known_at_four_of_seven_instants_and_unknown_at_three() -> None:
    four = _track([_NOW - 30.0, _NOW], lateral_m=3.0)
    three = _track([_NOW - 20.0, _NOW], lateral_m=3.0)

    known = estimate_together(_phone_a(), four, now=_NOW)
    unknown = estimate_together(_phone_a(), three, now=_NOW)

    assert known.known and known.windows[0].usable == 4
    assert unknown.unknown_reason is UnknownReason.TOO_LITTLE_COVERAGE and unknown.windows[0].usable == 3


def test_the_ramp_between_near_and_far_keeps_its_width_when_the_accuracies_are_large() -> None:
    params = TogetherParams(near_m=30.0, far_m=150.0)
    here = InterpolatedPosition(accuracy_m=50.0, lat=_LAT0, lon=_LON0)

    def at(distance_m: float) -> InterpolatedPosition:
        return InterpolatedPosition(accuracy_m=50.0, lat=_LAT0 + distance_m / _M_PER_DEG, lon=_LON0)

    # near = 130 m, far = 250 m: 190 m is the middle of the ramp, not a cliff.
    assert pair_closeness(here, at(125.0), params=params)[1] == 1.0
    assert 0.4 < pair_closeness(here, at(190.0), params=params)[1] < 0.6
    assert pair_closeness(here, at(260.0), params=params)[1] == 0.0


def _estimate(confidence: float | None, recent: float | None) -> TogetherEstimate:
    return TogetherEstimate(
        anchor=_NOW,
        anchor_lag_s=0.0,
        apart_basis=None,
        apart_for_s=None,
        confidence=confidence,
        distance_now_m=None,
        dropped_quality=(0, 0),
        dropped_speed=(0, 0),
        newest_fix_age_s=(0.0, 0.0),
        recent_closeness=recent,
        together_for_s=None,
        unknown_reason=None,
        unknown_track=None,
        windows=(),
    )


def test_is_together_needs_both_the_confidence_and_the_last_minute_at_the_minimum() -> None:
    assert _estimate(0.8, 0.8).is_together(0.8) is True
    assert _estimate(0.9, 0.79).is_together(0.8) is False
    assert _estimate(0.79, 0.9).is_together(0.8) is False
    assert _estimate(None, None).is_together(0.8) is None


def test_is_apart_is_symmetric_and_leaves_a_dead_zone_in_between() -> None:
    assert _estimate(0.2, 0.2).is_apart(0.8) is True
    assert _estimate(0.2, 0.3).is_apart(0.8) is False
    assert _estimate(0.5, 0.5).is_apart(0.8) is False
    assert _estimate(0.5, 0.5).is_together(0.8) is False
    assert _estimate(None, None).is_apart(0.8) is None


def test_is_recently_apart_looks_only_at_the_last_minute() -> None:
    assert _estimate(0.6, 0.2).is_recently_apart(0.8) is True
    assert _estimate(0.1, 0.3).is_recently_apart(0.8) is False
    assert _estimate(None, None).is_recently_apart(0.8) is None


def test_apart_for_s_counts_back_over_known_clearly_apart_instants_only() -> None:
    now_apart = estimate_together(
        _phone_a(),
        [_fix(at, lateral_m=3.0 if at < _NOW - 100.0 else 400.0) for at in _times(every_s=10.0, last_at=_NOW)],
        now=_NOW,
    )
    together_now = estimate_together(_phone_a(), _track(_times(every_s=10.0, last_at=_NOW), lateral_m=3.0), now=_NOW)
    short = estimate_together(
        _track(_times(every_s=10.0, last_at=_NOW, span_s=70.0)),
        _track(_times(every_s=10.0, last_at=_NOW, span_s=70.0), lateral_m=400.0),
        now=_NOW,
    )

    assert now_apart.apart_for_s is not None and 80.0 <= now_apart.apart_for_s <= 110.0
    assert together_now.apart_for_s == 0.0
    assert short.apart_for_s is not None and short.apart_for_s <= 70.0


def _home_wifi(fixes: list[TrackFix], *, on: bool = True) -> list[TrackFix]:
    return [replace(fix, home_wifi=on) for fix in fixes]


def test_a_peer_on_the_home_network_is_apart_from_a_user_arriving_from_outside() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0, 1_500.0, 2_400.0]))

    estimate = estimate_together(_phone_a(), peer, now=_NOW)

    assert estimate.known
    assert estimate.apart_basis is ApartBasis.HOME_WIFI
    assert estimate.is_apart(0.8) is True
    assert estimate.distance_now_m == pytest.approx(250.0, abs=2.0)


def test_one_recent_report_on_the_home_network_is_enough() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is ApartBasis.HOME_WIFI


def test_the_home_network_may_be_the_first_user() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))

    assert estimate_together(peer, _phone_a(), now=_NOW).apart_basis is ApartBasis.HOME_WIFI


def test_a_home_network_report_older_than_twenty_minutes_is_not_evidence() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[1_300.0]))

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_two_users_on_the_home_network_are_not_apart_by_it() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))

    estimate = estimate_together(_home_wifi(_phone_a()), peer, now=_NOW)

    assert estimate.apart_basis is not ApartBasis.HOME_WIFI


def test_a_peer_close_to_the_other_user_is_not_apart_by_the_home_network() -> None:
    peer = _home_wifi(_repeated_fix(north_m=100.0, fix_age_s=11_000.0, report_ages_s=[600.0]))

    estimate = estimate_together(_phone_a(), peer, now=_NOW)

    assert estimate.apart_basis is not ApartBasis.HOME_WIFI
    assert estimate.unknown_reason is UnknownReason.STALE_FIX


def test_the_newest_report_decides_whether_the_peer_is_on_the_home_network() -> None:
    older = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[1_100.0]))
    newer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]), on=False)

    assert estimate_together(_phone_a(), [*older, *newer], now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_an_arriving_user_whose_fix_is_stale_is_not_apart_by_the_home_network() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 120.0))

    assert estimate_together(arriving, peer, now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_a_stale_peer_that_was_not_close_in_the_last_minutes_is_apart() -> None:
    peer = _repeated_fix(north_m=5_000.0, fix_age_s=120.0, report_ages_s=[100.0])

    estimate = estimate_together(_phone_a(), peer, now=_NOW)

    assert estimate.known
    assert estimate.apart_basis is ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate.is_apart(0.8) is True
    assert estimate.is_together(0.8) is False


def test_a_stale_peer_that_was_close_within_the_window_stays_unknown() -> None:
    peer = _repeated_fix(north_m=0.0, fix_age_s=120.0, report_ages_s=[100.0])

    estimate = estimate_together(_phone_a(), peer, now=_NOW)

    assert not estimate.known
    assert estimate.unknown_reason is UnknownReason.STALE_FIX


def test_closeness_older_than_the_window_does_not_count() -> None:
    # The user drove past this spot about 350 s ago: outside the default 300 s window.
    passed_long_ago = _repeated_fix(north_m=-7_000.0, fix_age_s=11_000.0, report_ages_s=[100.0])
    passed_inside_window = _repeated_fix(north_m=-4_000.0, fix_age_s=11_000.0, report_ages_s=[100.0])

    assert estimate_together(_phone_a(), passed_long_ago, now=_NOW).apart_basis is ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate_together(_phone_a(), passed_inside_window, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_the_recent_closeness_window_is_a_parameter() -> None:
    passed_inside_window = _repeated_fix(north_m=-4_000.0, fix_age_s=11_000.0, report_ages_s=[100.0])

    estimate = estimate_together(
        _phone_a(), passed_inside_window, now=_NOW, params=TogetherParams(recent_close_window_s=60.0)
    )

    assert estimate.apart_basis is ApartBasis.NOT_RECENTLY_CLOSE


def test_sparse_reports_far_apart_are_apart_not_unknown() -> None:
    sparse = _track(_times(every_s=60.0, last_at=_NOW - 5.0), lateral_m=500.0)

    estimate = estimate_together(_phone_a(), sparse, now=_NOW)

    assert estimate.apart_basis is ApartBasis.NOT_RECENTLY_CLOSE


def test_sparse_reports_close_together_stay_unknown() -> None:
    sparse = _track(_times(every_s=60.0, last_at=_NOW - 5.0), lateral_m=3.0)

    estimate = estimate_together(_phone_a(), sparse, now=_NOW)

    assert estimate.unknown_reason is UnknownReason.TOO_LITTLE_COVERAGE


@pytest.mark.parametrize("window_s", [0.0, -1.0, math.nan, math.inf])
def test_the_recent_closeness_window_must_be_positive_and_finite(window_s: float) -> None:
    with pytest.raises(ValueError, match="recent_close_window_s"):
        TogetherParams(recent_close_window_s=window_s)


def test_a_home_wifi_report_without_an_accuracy_is_not_evidence() -> None:
    peer = [
        replace(fix, accuracy_m=None)
        for fix in _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))
    ]

    assert estimate_together(_phone_a(), peer, now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_a_newer_coarse_fix_near_the_other_user_keeps_a_stale_pair_unknown() -> None:
    peer = _repeated_fix(north_m=5_000.0, fix_age_s=120.0, report_ages_s=[100.0])
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 30.0))
    coarse_near_peer = _coarse_fix(_NOW - 5.0, north_m=5_000.0)

    assert estimate_together(arriving, peer, now=_NOW).apart_basis is ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate_together([*arriving, coarse_near_peer], peer, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_a_newer_coarse_fix_far_from_the_other_user_does_not_block_the_verdict() -> None:
    peer = _repeated_fix(north_m=5_000.0, fix_age_s=120.0, report_ages_s=[100.0])
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 30.0))
    coarse_far = _coarse_fix(_NOW - 5.0, north_m=-3_000.0)

    assert estimate_together([*arriving, coarse_far], peer, now=_NOW).apart_basis is ApartBasis.NOT_RECENTLY_CLOSE


def test_two_sparse_phones_in_one_moving_car_are_not_called_apart() -> None:
    # Each phone reports once a minute, offset by half a minute: no instant has two bracketing fixes within 45 s.
    phone_a = _track(_times(every_s=60.0, last_at=_NOW - 5.0), lateral_m=3.0, speed_mps=15.0)
    phone_b = _track(_times(every_s=60.0, last_at=_NOW - 35.0), lateral_m=3.0, speed_mps=15.0)

    estimate = estimate_together(phone_a, phone_b, now=_NOW)

    assert estimate.apart_basis is not ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate.unknown_reason is UnknownReason.TOO_LITTLE_COVERAGE


def test_a_home_wifi_peer_is_not_apart_when_the_other_user_was_just_near_its_reported_position() -> None:
    # The other user drifted in from the peer's own position a moment ago: they left together, not independently.
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))
    just_left_the_peer = _track(_times(every_s=10.0, last_at=_NOW, span_s=600.0), speed_mps=-2.5)

    estimate = estimate_together(just_left_the_peer, peer, now=_NOW)

    assert estimate.apart_basis is None
    assert estimate.unknown_reason is UnknownReason.STALE_FIX


def test_a_coarse_newest_fix_of_the_away_user_is_not_the_position_for_the_home_wifi_check() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 20.0))
    coarse_newer = _coarse_fix(_NOW - 5.0, north_m=5_000.0)

    assert estimate_together(arriving, peer, now=_NOW).apart_basis is ApartBasis.HOME_WIFI
    assert estimate_together([*arriving, coarse_newer], peer, now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_a_dropped_fix_at_the_newest_fix_time_is_still_checked_for_closeness() -> None:
    peer = _repeated_fix(north_m=5_000.0, fix_age_s=120.0, report_ages_s=[100.0])
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 30.0))
    twin = TrackFix(accuracy_m=30.0, at=arriving[-1].at, lat=_LAT0 + 5_000.0 / _M_PER_DEG, lon=_LON0)

    assert estimate_together(arriving, peer, now=_NOW).apart_basis is ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate_together([*arriving, twin], peer, now=_NOW).unknown_reason is UnknownReason.STALE_FIX


def test_a_dropped_twin_at_the_away_users_fix_time_vetoes_the_home_wifi_basis() -> None:
    peer = _home_wifi(_repeated_fix(north_m=250.0, fix_age_s=11_000.0, report_ages_s=[600.0]))
    arriving = _track(_times(every_s=10.0, last_at=_NOW - 20.0))
    # A less accurate fix at the same instant is dropped by the cleaning but sits next to the peer.
    twin = TrackFix(accuracy_m=30.0, at=arriving[-1].at, lat=_LAT0 + 250.0 / _M_PER_DEG, lon=_LON0)

    assert estimate_together(arriving, peer, now=_NOW).apart_basis is ApartBasis.HOME_WIFI
    assert estimate_together([*arriving, twin], peer, now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


def test_a_dropped_report_stays_inconclusive_when_the_other_user_has_no_position_yet() -> None:
    peer = _repeated_fix(north_m=5_000.0, fix_age_s=90.0, report_ages_s=[80.0])
    quiet = [_fix(_NOW - 200.0), _coarse_fix(_NOW - 100.0, north_m=5_000.0)]
    without_the_coarse_report = quiet[:1]

    assert estimate_together(without_the_coarse_report, peer, now=_NOW).apart_basis is ApartBasis.NOT_RECENTLY_CLOSE
    assert estimate_together(quiet, peer, now=_NOW).unknown_reason is UnknownReason.STALE_FIX
