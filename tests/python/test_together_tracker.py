"""The together tracker turns estimates into one begin line and one end line per episode."""

from __future__ import annotations

import math

import pytest

from app.together_tracker import (
    TogetherBegan,
    TogetherEnded,
    TogetherKey,
    TogetherTracker,
    format_together_event,
)
from app.users_together import TogetherEstimate, TrackFix, estimate_together

_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_LAT0 = 41.0
_LON0 = -73.9
_KEY: TogetherKey = (("user-a", "user-b"), 0.8)
_START = 1_000_000.0


def _fix(at: float, *, lateral_m: float = 0.0, accuracy_m: float = 5.0) -> TrackFix:
    lat = _LAT0 + 20.0 * (at - _START) / _M_PER_DEG
    lon = _LON0 + lateral_m / (_M_PER_DEG * math.cos(math.radians(_LAT0)))
    return TrackFix(accuracy_m=accuracy_m, at=at, lat=lat, lon=lon)


def _estimate(
    now: float,
    *,
    apart_after: float | None = None,
    apart_before: float | None = None,
    lateral_m: float = 400.0,
    stale_b_s: float = 0.0,
) -> TogetherEstimate:
    """Two phones reporting every 10 s since the start; user B is ``lateral_m`` away outside the together span."""
    track_a = [_fix(at) for at in _times(now)]
    track_b: list[TrackFix] = []
    for at in _times(now - stale_b_s):
        far = (apart_after is not None and at > apart_after) or (apart_before is not None and at < apart_before)
        track_b.append(_fix(at, lateral_m=lateral_m if far else 3.0))
    return estimate_together(track_a, track_b, now=now)


def _times(last_at: float) -> list[float]:
    first = max(_START, last_at - 700.0)
    count = int((last_at - first) // 10.0)
    return [last_at - 10.0 * index for index in range(count, -1, -1)]


def test_a_pair_that_is_together_produces_one_begin_event_and_then_nothing() -> None:
    tracker = TogetherTracker()

    first = tracker.observe(_KEY, _estimate(_START + 700.0))
    later = [tracker.observe(_KEY, _estimate(_START + 700.0 + 10.0 * step)) for step in range(1, 6)]

    assert isinstance(first, TogetherBegan)
    assert first.users == ("user-a", "user-b")
    assert first.confidence >= 0.95
    assert first.first_sight is True
    assert later == [None] * 5


def test_the_begin_event_says_when_the_evidence_starts_and_when_it_is_capped_by_the_horizon() -> None:
    tracker = TogetherTracker()
    apart_before = _START + 460.0

    event = tracker.observe(_KEY, _estimate(_START + 700.0, apart_before=apart_before))

    assert isinstance(event, TogetherBegan)
    assert event.horizon_capped is False
    assert event.since == pytest.approx(apart_before, abs=30.0)
    capped = TogetherTracker().observe(_KEY, _estimate(_START + 700.0))
    assert isinstance(capped, TogetherBegan) and capped.horizon_capped is True


def test_a_pair_first_seen_apart_and_later_together_is_not_reported_as_already_in_progress() -> None:
    tracker = TogetherTracker()

    assert tracker.observe(_KEY, _estimate(_START + 300.0, apart_before=_START + 10_000.0)) is None
    began = tracker.observe(_KEY, _estimate(_START + 700.0, apart_before=_START + 300.0))

    assert isinstance(began, TogetherBegan)
    assert began.first_sight is False


def test_an_episode_ends_once_the_last_minute_is_clearly_apart_and_dates_the_end_at_the_split() -> None:
    tracker = TogetherTracker()
    events = []
    split = _START + 700.0
    for now in range(int(_START + 700.0), int(_START + 700.0) + 180, 10):
        events.append(tracker.observe(_KEY, _estimate(float(now), apart_after=split)))

    kinds = [type(event).__name__ for event in events if event is not None]
    assert kinds == ["TogetherBegan", "TogetherEnded"]
    ended = next(event for event in events if isinstance(event, TogetherEnded))
    assert ended.ended_at == pytest.approx(split + 10.0, abs=20.0)
    assert ended.detected_at > ended.ended_at
    assert ended.detected_at - split < 90.0
    assert ended.duration_s == pytest.approx(ended.ended_at - ended.began_at)
    assert ended.duration_s > 600.0
    assert ended.peak >= 0.95 and ended.low < ended.peak
    assert ended.min_confidence == 0.8


def test_the_dead_zone_neither_starts_nor_ends_an_episode() -> None:
    tracker = TogetherTracker()
    steady_hundred_meters = _estimate(_START + 700.0, apart_before=_START + 10_000.0, lateral_m=100.0)

    assert tracker.observe(_KEY, steady_hundred_meters) is None
    assert isinstance(tracker.observe(_KEY, _estimate(_START + 700.0)), TogetherBegan)
    assert tracker.observe(_KEY, steady_hundred_meters) is None
    assert tracker.observe(_KEY, _estimate(_START + 710.0, apart_after=_START + 700.0)) is None
    assert isinstance(tracker.observe(_KEY, _estimate(_START + 770.0, apart_after=_START + 700.0)), TogetherEnded)


def test_missing_or_stale_data_never_ends_an_episode_and_is_counted() -> None:
    tracker = TogetherTracker()
    assert isinstance(tracker.observe(_KEY, _estimate(_START + 700.0)), TogetherBegan)

    stale = _estimate(_START + 800.0, stale_b_s=300.0)
    assert not stale.known
    assert [tracker.observe(_KEY, stale) for _ in range(3)] == [None, None, None]
    assert tracker.observe(_KEY, _estimate(_START + 810.0)) is None

    ended = tracker.observe(_KEY, _estimate(_START + 830.0, apart_after=_START + 700.0))
    assert isinstance(ended, TogetherEnded)
    assert ended.unknown_evaluations == 3


def test_unknown_data_before_any_episode_starts_nothing() -> None:
    tracker = TogetherTracker()

    assert tracker.observe(_KEY, _estimate(_START + 800.0, stale_b_s=300.0)) is None
    assert isinstance(tracker.observe(_KEY, _estimate(_START + 810.0)), TogetherBegan)


def test_forgetting_a_pair_makes_the_next_sighting_a_first_sight_again() -> None:
    tracker = TogetherTracker()
    assert isinstance(tracker.observe(_KEY, _estimate(_START + 700.0)), TogetherBegan)

    tracker.retain([])
    again = tracker.observe(_KEY, _estimate(_START + 710.0))

    assert isinstance(again, TogetherBegan)
    assert again.first_sight is True


def test_a_pair_judged_by_two_thresholds_is_tracked_independently() -> None:
    tracker = TogetherTracker()
    strict: TogetherKey = (("user-a", "user-b"), 0.99)
    steady = _estimate(_START + 700.0, apart_before=_START + 10_000.0, lateral_m=60.0)

    assert isinstance(tracker.observe(_KEY, steady), TogetherBegan)
    assert tracker.observe(strict, steady) is None


def test_the_log_lines_carry_the_values_an_operator_tunes_from() -> None:
    tracker = TogetherTracker()
    began = tracker.observe(_KEY, _estimate(_START + 700.0))
    assert isinstance(began, TogetherBegan)
    began_line = format_together_event(began, names="User A and User B", rule_ids=["z-rule", "a-rule"])

    assert began_line.startswith("[rules] together began users=user-a,user-b names='User A and User B' since=")
    for fragment in ("confidence=1.00", "closeness_1m=1.00", "closeness_5m=1.00", "closeness_10m=1.00"):
        assert fragment in began_line
    assert "min_confidence=0.80 first_sight=true horizon_capped=true rules=a-rule,z-rule" in began_line

    ended = tracker.observe(_KEY, _estimate(_START + 780.0, apart_after=_START + 700.0))
    assert isinstance(ended, TogetherEnded)
    ended_line = format_together_event(ended, names="User A and User B", rule_ids=["a-rule"])
    assert " detected=" in ended_line
    assert "[rules] together ended users=user-a,user-b" in ended_line
    assert "reason=clearly_apart rules=a-rule" in ended_line
    assert "unknown_evaluations=0" in ended_line


def test_first_sight_means_the_first_time_the_pair_could_be_evaluated_not_the_first_attempt() -> None:
    tracker = TogetherTracker()

    assert tracker.observe(_KEY, _estimate(_START + 800.0, stale_b_s=300.0)) is None
    began = tracker.observe(_KEY, _estimate(_START + 810.0))

    assert isinstance(began, TogetherBegan)
    assert began.first_sight is True
