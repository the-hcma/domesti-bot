"""The home Wi-Fi apart basis: a scenario matrix checked against an independent oracle, plus seeded properties.

Field cases behind it (domesti-bot#940): the peer's phone re-sends one stale, coarse fix from the home Wi-Fi while the
other user drives home. The basis must settle that pair as apart whatever the length of the arriving user's track,
and must never call a pair apart because one phone joined the home Wi-Fi after the two arrived together.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, replace

import pytest

from app.users_together import ApartBasis, TogetherParams, TrackFix, estimate_together

_NOW = 100_000.0
_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_LAT0 = 41.0
_LON0 = -73.9
_PARAMS = TogetherParams()
_FAR_M = _PARAMS.far_m
_FRESH_REPORT_S = 1_200.0
_TRACK_TOLERANCE_S = 300.0
_ARRIVING_ACCURACY_M = 5.0
_STALE_FIX_AGE_S = 117_800.0
_FRESH_FIX_AGE_S = 1.0


@dataclass(frozen=True)
class Scenario:
    """A phone at home on the Wi-Fi and a user driving toward it, both described by what the estimator is given."""

    arriving_on_home_wifi: bool = False
    arriving_speed_mps: float = 0.0
    distance_m: float = 400.0
    peer_accuracy_m: float | None = 100.0
    peer_fix_age_s: float = _STALE_FIX_AGE_S
    report_ages_s: tuple[float, ...] = (240.0,)
    track_span_s: float = 600.0

    def arriving_track(self) -> list[TrackFix]:
        """Fixes every 10 s over ``track_span_s``.

        ``distance_m`` is the distance from the peer at the fix closest in time to the newest report, which is the
        position the estimator judges: when the track starts after that report it is the oldest fix, never an
        extrapolation to the report time.
        """
        count = int(self.track_span_s // 10.0)
        anchor_age = min(max(round(min(self.report_ages_s) / 10.0) * 10.0, 0.0), count * 10.0)
        fixes: list[TrackFix] = []
        for index in range(count, -1, -1):
            age = index * 10.0
            # Uniform motion toward the peer: closer by speed * time elapsed since the anchor fix.
            north_m = self.distance_m - self.arriving_speed_mps * (anchor_age - age)
            fixes.append(
                TrackFix(
                    accuracy_m=_ARRIVING_ACCURACY_M,
                    at=_NOW - age,
                    home_wifi=self.arriving_on_home_wifi,
                    lat=_LAT0 + north_m / _M_PER_DEG,
                    lon=_LON0,
                    reported_at=_NOW - age + 1.0,
                )
            )
        return fixes

    def peer_track(self) -> list[TrackFix]:
        """One report per entry of ``report_ages_s`` from the same position on the home Wi-Fi."""
        return [
            TrackFix(
                accuracy_m=self.peer_accuracy_m,
                at=_NOW - age - self.peer_fix_age_s if self.peer_fix_age_s > _FRESH_FIX_AGE_S else _NOW - age - 1.0,
                home_wifi=True,
                lat=_LAT0,
                lon=_LON0,
                reported_at=_NOW - age,
            )
            for age in self.report_ages_s
        ]

    def oracle_is_apart_by_home_wifi(self) -> bool:
        """The contract in the issue, written independently of the implementation."""
        newest_age = min(self.report_ages_s)
        if newest_age > _FRESH_REPORT_S or self.peer_accuracy_m is None:
            return False
        if self.arriving_on_home_wifi:
            return False
        # The arriving user's track must reach to within the tolerance of the newest report time.
        if newest_age - self.track_span_s > _TRACK_TOLERANCE_S:
            return False
        arriving_accuracy = _ARRIVING_ACCURACY_M
        return self.distance_m - (self.peer_accuracy_m + arriving_accuracy) >= _FAR_M


def _estimate(scenario: Scenario, *, peer_first: bool = False):  # noqa: ANN202 - the estimate type is internal
    """Estimate the pair of ``scenario``, with the peer as the first or the second member."""
    arriving, peer = scenario.arriving_track(), scenario.peer_track()
    pair = (peer, arriving) if peer_first else (arriving, peer)
    return estimate_together(pair[0], pair[1], now=_NOW)


_REPORT_AGE_SETS = [
    (60.0,),
    (240.0,),
    (1_100.0,),
    (1_199.0,),
    (1_201.0,),
    (240.0, 1_100.0),
    (60.0, 500.0, 1_100.0),
    (240.0, 1_500.0),
]
_ACCURACIES = [None, 5.0, 48.0, 100.0]
_DISTANCES = [100.0, 254.0, 256.0, 400.0, 1_500.0]
_SPANS = [60.0, 100.0, 600.0, 1_200.0]


def _matrix() -> list[Scenario]:
    """Every combination of the axes the contract depends on."""
    scenarios: list[Scenario] = []
    for ages, accuracy, distance, span, fix_age in itertools.product(
        _REPORT_AGE_SETS, _ACCURACIES, _DISTANCES, _SPANS, (_STALE_FIX_AGE_S, _FRESH_FIX_AGE_S)
    ):
        scenarios.append(
            Scenario(
                distance_m=distance,
                peer_accuracy_m=accuracy,
                peer_fix_age_s=fix_age,
                report_ages_s=ages,
                track_span_s=span,
            )
        )
    return scenarios


def _scenario_id(scenario: Scenario) -> str:
    """A readable test id naming the axes of ``scenario``."""
    kind = "stale" if scenario.peer_fix_age_s > _FRESH_FIX_AGE_S else "fresh"
    return (
        f"reports{'+'.join(str(int(a)) for a in scenario.report_ages_s)}-acc{scenario.peer_accuracy_m}-{kind}"
        f"-dist{int(scenario.distance_m)}-span{int(scenario.track_span_s)}"
    )


@pytest.mark.parametrize("scenario", _matrix(), ids=_scenario_id)
def test_the_home_wifi_basis_matches_the_contract_across_the_scenario_matrix(scenario: Scenario) -> None:
    estimate = _estimate(scenario)

    assert (estimate.apart_basis is ApartBasis.HOME_WIFI) is scenario.oracle_is_apart_by_home_wifi()


@pytest.mark.parametrize(
    "scenario",
    [
        Scenario(),
        Scenario(report_ages_s=(240.0, 1_100.0)),
        Scenario(peer_accuracy_m=48.0, distance_m=135.0 + 48.0 + 5.0 + 150.0),
        Scenario(peer_fix_age_s=_FRESH_FIX_AGE_S, peer_accuracy_m=5.0, distance_m=170.0),
    ],
    ids=["stale-100m", "two-reports", "coarse-48m", "fresh-fix"],
)
def test_the_home_wifi_user_may_be_either_member_of_the_pair(scenario: Scenario) -> None:
    first = _estimate(scenario)
    swapped = _estimate(scenario, peer_first=True)

    assert first.apart_basis is ApartBasis.HOME_WIFI
    assert swapped.apart_basis is ApartBasis.HOME_WIFI
    assert first.distance_now_m == pytest.approx(swapped.distance_now_m)


def test_the_field_case_with_ten_minutes_of_track_is_apart_by_the_home_wifi() -> None:
    # 2026-11-06 21:23 EST: two re-sent 32-hour-old 100 m fixes from the home Wi-Fi, 4 and 19 minutes before the
    # arrival, and 10 minutes of the arriving user's track.
    scenario = Scenario(arriving_speed_mps=2.5, distance_m=1_500.0, report_ages_s=(240.0, 1_146.0), track_span_s=600.0)

    estimate = _estimate(scenario)

    assert estimate.apart_basis is ApartBasis.HOME_WIFI
    assert estimate.is_apart(0.8) is True


def test_the_arriving_user_joining_the_home_wifi_after_arriving_together_is_not_apart() -> None:
    # 2026-10-13 17:06 EDT: both phones in one car in the driveway, one of them joined the home Wi-Fi first. The
    # phone that joined reported only after the car arrived, so the other user was 14 m from it when it reported.
    joined = Scenario(distance_m=14.0, peer_accuracy_m=12.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(30.0,))

    estimate = _estimate(joined)

    assert estimate.apart_basis is not ApartBasis.HOME_WIFI
    assert estimate.is_apart(0.8) is not True


def test_the_other_user_on_the_home_wifi_too_is_never_apart_by_it() -> None:
    estimate = _estimate(Scenario(arriving_on_home_wifi=True))

    assert estimate.apart_basis is not ApartBasis.HOME_WIFI


@pytest.mark.parametrize("sharp_first", [True, False], ids=["sharp-listed-first", "coarse-listed-first"])
def test_rows_tied_at_the_newest_report_time_are_judged_by_the_least_precise_one(sharp_first: bool) -> None:
    # One delivery stored twice with different accuracies: the 1000 m row cannot establish a separation from a user
    # 400 m away, so the 5 m row must not either.
    scenario = Scenario(distance_m=400.0, peer_accuracy_m=5.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(240.0,))
    (sharp,) = scenario.peer_track()
    coarse = replace(sharp, accuracy_m=1_000.0)
    peer = [sharp, coarse] if sharp_first else [coarse, sharp]

    tied = estimate_together(scenario.arriving_track(), peer, now=_NOW)
    alone = estimate_together(scenario.arriving_track(), [sharp], now=_NOW)

    assert alone.apart_basis is ApartBasis.HOME_WIFI
    assert tied.apart_basis is not ApartBasis.HOME_WIFI


def test_a_tied_report_without_an_accuracy_leaves_the_basis_unproven() -> None:
    scenario = Scenario(distance_m=400.0, peer_accuracy_m=5.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(240.0,))
    (sharp,) = scenario.peer_track()

    tied = estimate_together(scenario.arriving_track(), [sharp, replace(sharp, accuracy_m=None)], now=_NOW)

    assert tied.apart_basis is not ApartBasis.HOME_WIFI


def test_a_coarse_fix_near_the_peer_at_the_report_time_is_not_outvoted_by_a_sharper_far_one() -> None:
    # The arriving user's good fixes put them 600 m away, but a 200 m fix made at the report time, which cleaning
    # drops for its accuracy, puts them at the peer's position: the evidence conflicts, so nothing is proven.
    scenario = Scenario(distance_m=600.0, peer_accuracy_m=5.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(240.0,))
    coarse_near = TrackFix(accuracy_m=200.0, at=_NOW - 240.0, lat=_LAT0, lon=_LON0, reported_at=_NOW - 239.0)
    arriving = [*scenario.arriving_track(), coarse_near]

    assert estimate_together(scenario.arriving_track(), scenario.peer_track(), now=_NOW).apart_basis is (
        ApartBasis.HOME_WIFI
    )
    assert estimate_together(arriving, scenario.peer_track(), now=_NOW).apart_basis is not ApartBasis.HOME_WIFI


@pytest.mark.parametrize("accuracy", [None, math.nan, -1.0, math.inf], ids=["missing", "nan", "negative", "infinite"])
def test_a_dropped_fix_at_the_report_time_with_an_unusable_accuracy_counts_as_possibly_near(
    accuracy: float | None,
) -> None:
    # Cleaning drops a fix with no usable accuracy, and nothing is known about how far off it is, so a position near
    # the peer at the report time must never be read as far.
    scenario = Scenario(distance_m=600.0, peer_accuracy_m=5.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(240.0,))
    unknown_near = TrackFix(accuracy_m=accuracy, at=_NOW - 240.0, lat=_LAT0, lon=_LON0, reported_at=_NOW - 239.0)

    estimate = estimate_together([*scenario.arriving_track(), unknown_near], scenario.peer_track(), now=_NOW)

    assert estimate.apart_basis is not ApartBasis.HOME_WIFI


def test_a_coarse_fix_far_from_the_peer_at_the_report_time_does_not_block_the_basis() -> None:
    scenario = Scenario(distance_m=600.0, peer_accuracy_m=5.0, peer_fix_age_s=_FRESH_FIX_AGE_S, report_ages_s=(240.0,))
    coarse_far = TrackFix(
        accuracy_m=200.0, at=_NOW - 240.0, lat=_LAT0 + 5_000.0 / _M_PER_DEG, lon=_LON0, reported_at=_NOW - 239.0
    )

    estimate = estimate_together([*scenario.arriving_track(), coarse_far], scenario.peer_track(), now=_NOW)

    assert estimate.apart_basis is ApartBasis.HOME_WIFI


def _random_scenario(rng: random.Random) -> Scenario:
    """A scenario drawn from ``rng`` across the same axes as the matrix, with moving tracks as well."""
    ages = sorted({round(rng.uniform(5.0, 1_400.0), 1) for _ in range(rng.randint(1, 4))})
    return Scenario(
        arriving_speed_mps=rng.choice([0.0, 2.5, 12.0, 20.0]),
        distance_m=rng.choice([0.0, 20.0, 150.0, 254.0, 256.0, 400.0, 3_000.0]) + rng.uniform(0.0, 3.0),
        peer_accuracy_m=rng.choice([None, 3.0, 19.0, 48.0, 100.0]),
        peer_fix_age_s=rng.choice([_STALE_FIX_AGE_S, _FRESH_FIX_AGE_S]),
        report_ages_s=tuple(ages),
        track_span_s=rng.choice([60.0, 300.0, 600.0, 900.0, 1_200.0]),
    )


_SEEDS = list(range(40))


@pytest.mark.parametrize("seed", _SEEDS)
def test_property_the_basis_is_never_used_when_the_other_user_was_near_the_newest_report(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        scenario = replace(_random_scenario(rng), distance_m=rng.uniform(0.0, 200.0), peer_accuracy_m=100.0)
        assert _estimate(scenario).apart_basis is not ApartBasis.HOME_WIFI, scenario


@pytest.mark.parametrize("seed", _SEEDS)
def test_property_older_reports_of_the_run_never_change_the_verdict(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        scenario = _random_scenario(rng)
        newest = min(scenario.report_ages_s)
        alone = replace(scenario, report_ages_s=(newest,))
        base = _estimate(alone)
        with_older = _estimate(scenario)
        assert (with_older.apart_basis is ApartBasis.HOME_WIFI) == (base.apart_basis is ApartBasis.HOME_WIFI), scenario


@pytest.mark.parametrize("seed", _SEEDS)
def test_property_swapping_the_users_never_changes_the_verdict(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        scenario = _random_scenario(rng)
        first, swapped = _estimate(scenario), _estimate(scenario, peer_first=True)
        assert first.apart_basis == swapped.apart_basis, scenario
        assert first.unknown_reason == swapped.unknown_reason, scenario


@pytest.mark.parametrize("seed", _SEEDS)
def test_property_a_farther_arriving_user_never_loses_the_basis(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        scenario = _random_scenario(rng)
        if _estimate(scenario).apart_basis is not ApartBasis.HOME_WIFI:
            continue
        farther = replace(scenario, distance_m=scenario.distance_m + rng.uniform(1.0, 5_000.0))
        assert _estimate(farther).apart_basis is ApartBasis.HOME_WIFI, scenario


@pytest.mark.parametrize("seed", _SEEDS)
def test_property_the_estimator_always_returns_a_consistent_estimate(seed: int) -> None:
    rng = random.Random(seed)
    for _ in range(25):
        scenario = _random_scenario(rng)
        estimate = _estimate(scenario)
        if estimate.apart_basis is not None:
            assert estimate.known
            assert estimate.is_apart(0.8) is True
            assert estimate.is_together(0.8) is False
        if estimate.unknown_reason is not None:
            assert not estimate.known
        assert estimate.confidence is None or 0.0 <= estimate.confidence <= 1.0
