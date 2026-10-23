"""Estimate how likely two users are travelling together from their stored location tracks.

Phones report at different moments and a car covers 200 to 400 m between two
reports, so comparing each user's latest fix says nothing about proximity. This
module therefore works on whole tracks:

1. Low-quality fixes are dropped first (missing or poor accuracy, impossible
   jumps), so they can never anchor an interpolation.
2. Each user's position is estimated at common reference instants by linear
   interpolation between the two good fixes that bracket the instant. There is
   no extrapolation past a user's newest good fix, and no interpolation across a
   gap longer than ``max_gap_s``.
3. Each instant becomes a closeness in ``[0, 1]`` (full credit within ``near_m``
   plus the two fixes' accuracies, none beyond ``far_m``).
4. Several windows ending at the same anchor (by default 1, 5 and 10 minutes)
   each give a mean closeness and a coverage. A window with too little coverage
   is unknown, not "apart".
5. The confidence is the weighted mean of the known windows' closeness. The
   windows overlap, so they are averaged, never multiplied.

A track with no good fix at all is unknown, with one exception: when its newest
fix, however coarse, is farther from the other user's position than both
accuracy circles and the ground a car could have covered since (``max_speed_mps``)
allow within ``far_m``, the pair is apart. A phone on Wi-Fi hundreds of
kilometers away reports only coarse fixes and would otherwise never settle.

A user whose phone keeps re-sending one old fix (the fix time never changes while
reports keep arriving) has not moved. When the newest such report is recent and
that position is clearly far from the other user, the pair is apart, however old
the fix is.

A user whose newest report (at most ``_HOME_WIFI_REPORT_FRESH_S`` old) was made on the home Wi-Fi is at home. When
the other user's fresh fix is not on that network and is clearly far from the reported position, the pair is apart:
nobody arrives in the same vehicle as a person who is already home. Which reports count as on the home Wi-Fi is the
caller's decision (``TrackFix.home_wifi``).

When the estimate would otherwise be unknown because a fix is stale or the tracks are too sparse, the pair is apart
unless the two users were close at some moment in the last ``recent_close_window_s``, with each user's newest good fix
carried forward. A pair that has not been close for minutes is simply not together. A user with no good fix at all
stays unknown: there is no position to say they were not close.

The module is pure: it knows nothing about rules, users or geofences.
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


@dataclass(frozen=True, slots=True)
class WindowSpec:
    """One look-back window ending at the anchor, with its weight in the confidence."""

    length_s: float
    weight: float


DEFAULT_WINDOWS: Final[tuple[WindowSpec, ...]] = (
    WindowSpec(length_s=60.0, weight=0.5),
    WindowSpec(length_s=300.0, weight=0.3),
    WindowSpec(length_s=600.0, weight=0.2),
)
STATIONARY_REPORT_WINDOW_S: Final[float] = 3600.0


@dataclass(frozen=True, slots=True)
class TogetherParams:
    """Thresholds for :func:`estimate_together`; the defaults are proposals to tune."""

    accuracy_limit_m: float = 50.0
    coverage_floor: float = 0.5
    far_m: float = 150.0
    freshness_s: float = 45.0
    max_gap_s: float = 45.0
    max_speed_mps: float = 50.0
    near_m: float = 30.0
    recent_close_window_s: float = 300.0
    step_s: float = 10.0
    windows: tuple[WindowSpec, ...] = DEFAULT_WINDOWS

    def __post_init__(self) -> None:
        if not self.windows:
            raise ValueError("Expected at least one window, got none")
        positives = {
            "accuracy_limit_m": self.accuracy_limit_m,
            "freshness_s": self.freshness_s,
            "max_gap_s": self.max_gap_s,
            "max_speed_mps": self.max_speed_mps,
            "recent_close_window_s": self.recent_close_window_s,
            "step_s": self.step_s,
        }
        for name, value in positives.items():
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Expected {name} to be a finite number greater than 0, got {value}")
        if (
            not (math.isfinite(self.near_m) and math.isfinite(self.far_m))
            or self.near_m < 0
            or self.far_m <= self.near_m
        ):
            raise ValueError(f"Expected 0 <= near_m < far_m, got near_m={self.near_m} far_m={self.far_m}")
        if not 0.0 <= self.coverage_floor <= 1.0:
            raise ValueError(f"Expected coverage_floor in [0, 1], got {self.coverage_floor}")
        lengths = [window.length_s for window in self.windows]
        if not all(math.isfinite(length) for length in lengths) or lengths != sorted(lengths) or lengths[0] <= 0:
            raise ValueError(f"Expected positive window lengths in ascending order, got {lengths}")
        for length in lengths:
            steps = length / self.step_s
            if abs(steps - round(steps)) > 1e-9:
                raise ValueError(f"Expected window lengths that are multiples of step_s={self.step_s}, got {length}")
        if not all(math.isfinite(window.weight) and window.weight > 0 for window in self.windows):
            raise ValueError(f"Expected positive window weights, got {[w.weight for w in self.windows]}")


class ApartBasis(StrEnum):
    """Why a pair is apart without the interpolated windows."""

    COARSE_FIXES = "coarse_fixes"
    HOME_WIFI = "home_wifi"
    NOT_RECENTLY_CLOSE = "not_recently_close"
    STATIONARY_PEER = "stationary_peer"


@dataclass(frozen=True, slots=True)
class CleanTrack:
    """A track after quality filtering, oldest fix first, with how many fixes were dropped and why."""

    dropped_quality: int
    dropped_speed: int
    fixes: tuple[TrackFix, ...]


@dataclass(frozen=True, slots=True)
class InterpolatedPosition:
    """A user's estimated position at one instant; the accuracy is the worse of the two bracketing fixes."""

    accuracy_m: float
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class TogetherEstimate:
    """The outcome of :func:`estimate_together`.

    ``confidence`` is ``None`` when the estimate is unknown; ``unknown_reason``
    then says why and ``unknown_track`` (0 or 1) which track it concerns, when
    one track is to blame (``windows`` is empty for ``NO_GOOD_FIXES`` and
    ``STALE_FIX``).

    ``together_for_s`` and ``apart_for_s`` are how far back from the anchor the
    instants stay known and together (closeness of at least 0.5), or known and
    apart (below 0.5); unknown instants end either run.

    The windows end at the ``anchor``, the latest instant both users are covered,
    which can be up to ``freshness_s`` before ``now``: ``anchor_lag_s`` says how
    far, and ``distance_now_m`` is the distance at the anchor (``None`` when
    the instant has a gap for one user), not at ``now``.
    """

    anchor: float | None
    anchor_lag_s: float | None
    apart_basis: ApartBasis | None
    apart_for_s: float | None
    confidence: float | None
    distance_now_m: float | None
    dropped_quality: tuple[int, int]
    dropped_speed: tuple[int, int]
    newest_fix_age_s: tuple[float | None, float | None]
    recent_closeness: float | None
    together_for_s: float | None
    unknown_reason: UnknownReason | None
    unknown_track: int | None
    windows: tuple[WindowEstimate, ...]

    @property
    def known(self) -> bool:
        return self.confidence is not None

    def is_apart(self, min_confidence: float) -> bool | None:
        """``None`` when unknown; otherwise whether the confidence and the shortest window are both clearly low.

        "Clearly" is symmetric with :meth:`is_together`: at most ``1 - min_confidence``.
        Between the two thresholds the users are neither together nor apart, so a
        rule that needs either answer does not act on a guess.
        """
        if self.confidence is None or self.recent_closeness is None:
            return None
        ceiling = 1.0 - min_confidence + _COMPARE_TOLERANCE
        return self.confidence <= ceiling and self.recent_closeness <= ceiling

    def is_recently_apart(self, min_confidence: float) -> bool | None:
        """``None`` when unknown; otherwise whether the last minute alone is clearly apart.

        Unlike :meth:`is_apart` this ignores the 5 and 10 minute windows, which stay
        high for minutes after a real separation. It answers "are they apart now?",
        which is what ending a together episode needs.
        """
        if self.recent_closeness is None:
            return None
        return self.recent_closeness <= 1.0 - min_confidence + _COMPARE_TOLERANCE

    def is_together(self, min_confidence: float) -> bool | None:
        """``None`` when unknown; otherwise whether the confidence and the shortest window both reach the minimum."""
        if self.confidence is None or self.recent_closeness is None:
            return None
        floor = min_confidence - _COMPARE_TOLERANCE
        return self.confidence >= floor and self.recent_closeness >= floor


@dataclass(frozen=True, slots=True)
class TrackFix:
    """One reported position of a user: seconds since the epoch, degrees, and accuracy in meters.

    ``report_only`` marks a fix kept as evidence of a report (see the stationary-peer exception) that
    is older than the history the track is built from; it is never cleaned or interpolated.
    ``home_wifi`` says the phone made the report on the home Wi-Fi network.
    """

    accuracy_m: float | None
    at: float
    lat: float
    lon: float
    home_wifi: bool = False
    report_only: bool = False
    reported_at: float | None = None


class UnknownReason(StrEnum):
    """Why an estimate could not be made."""

    NO_GOOD_FIXES = "no_good_fixes"
    STALE_FIX = "stale_fix"
    TOO_LITTLE_COVERAGE = "too_little_coverage"


@dataclass(frozen=True, slots=True)
class WindowEstimate:
    """The evidence from one window: ``closeness`` is ``None`` when the window is unknown."""

    closeness: float | None
    coverage: float
    length_s: float
    total: int
    usable: int
    weight: float

    @property
    def known(self) -> bool:
        return self.closeness is not None


def clean_track(fixes: Sequence[TrackFix], *, params: TogetherParams) -> CleanTrack:
    """Drop low-quality fixes and return the rest oldest first.

    A fix is dropped for missing or poor accuracy, a non-finite or out-of-range
    value, a time shared with a more accurate fix, or an impossible speed (a
    spike: both neighbors imply more than ``max_speed_mps``, or the one neighbor
    of an end fix does). Quality is checked before sorting, so a bad timestamp
    can never disturb the order. Speed ignores the distance the two fixes' own
    accuracy circles could explain and counts at least one second between fixes.
    A cluster of two or more consecutive bad fixes is not detected as a spike; the
    accuracy filter is the primary protection.
    """
    dropped_quality = 0
    usable: list[TrackFix] = []
    for fix in fixes:
        if _is_good_quality(fix, params):
            usable.append(fix)
        else:
            dropped_quality += 1
    kept: list[TrackFix] = []
    for fix in sorted(usable, key=lambda item: item.at):
        if kept and kept[-1].at == fix.at:
            dropped_quality += 1
            if (fix.accuracy_m or 0.0) < (kept[-1].accuracy_m or 0.0):
                kept[-1] = fix
            continue
        kept.append(fix)
    before_spikes = len(kept)
    kept = _without_interior_spikes(kept, params.max_speed_mps)
    kept = _without_end_spikes(kept, params.max_speed_mps)
    return CleanTrack(
        dropped_quality=dropped_quality,
        dropped_speed=before_spikes - len(kept),
        fixes=tuple(kept),
    )


def estimate_together(
    track_a: Sequence[TrackFix],
    track_b: Sequence[TrackFix],
    *,
    now: float,
    params: TogetherParams | None = None,
) -> TogetherEstimate:
    """Estimate how likely the two tracks belong to people travelling together at ``now``."""
    settings = params if params is not None else TogetherParams()
    cleaned = (
        clean_track([fix for fix in track_a if not fix.report_only], params=settings),
        clean_track([fix for fix in track_b if not fix.report_only], params=settings),
    )
    dropped_quality = (cleaned[0].dropped_quality, cleaned[1].dropped_quality)
    dropped_speed = (cleaned[0].dropped_speed, cleaned[1].dropped_speed)
    newest = tuple(clean.fixes[-1].at if clean.fixes else None for clean in cleaned)
    ages = (
        None if newest[0] is None else now - newest[0],
        None if newest[1] is None else now - newest[1],
    )

    def unknown(reason: UnknownReason, track: int | None, windows: tuple[WindowEstimate, ...] = ()) -> TogetherEstimate:
        return TogetherEstimate(
            anchor=None,
            anchor_lag_s=None,
            apart_basis=None,
            apart_for_s=None,
            confidence=None,
            distance_now_m=None,
            dropped_quality=dropped_quality,
            dropped_speed=dropped_speed,
            newest_fix_age_s=ages,
            recent_closeness=None,
            together_for_s=None,
            unknown_reason=reason,
            unknown_track=track,
            windows=windows,
        )

    at_home = _apart_by_home_wifi((track_a, track_b), cleaned, now=now, params=settings)
    if at_home is not None:
        return _apart_estimate(ApartBasis.HOME_WIFI, at_home, now=now, quality=(dropped_quality, dropped_speed))
    for index in (0, 1):
        if newest[index] is None:
            coarse = _apart_by_coarse_fixes((track_a, track_b), cleaned, now=now, params=settings)
            if coarse is None:
                return unknown(UnknownReason.NO_GOOD_FIXES, index)
            return _apart_estimate(ApartBasis.COARSE_FIXES, coarse, now=now, quality=(dropped_quality, dropped_speed))
    for index in (0, 1):
        age = ages[index]
        if age is not None and age > settings.freshness_s:
            stationary = _apart_by_stationary_peer((track_a, track_b), cleaned, stale=index, now=now, params=settings)
            if stationary is None:
                not_close = _apart_by_no_recent_closeness((track_a, track_b), cleaned, now=now, params=settings)
                if not_close is None:
                    return unknown(UnknownReason.STALE_FIX, index)
                return _apart_estimate(
                    ApartBasis.NOT_RECENTLY_CLOSE, not_close, now=now, quality=(dropped_quality, dropped_speed)
                )
            return _apart_estimate(
                ApartBasis.STATIONARY_PEER, stationary, now=now, quality=(dropped_quality, dropped_speed)
            )

    fixes_a, fixes_b = cleaned[0].fixes, cleaned[1].fixes
    anchor = min(now, fixes_a[-1].at, fixes_b[-1].at)
    horizon_s = settings.windows[-1].length_s
    steps = round(horizon_s / settings.step_s) + 1
    instants = [anchor - index * settings.step_s for index in range(steps)]
    closeness_at: list[float | None] = []
    distance_now_m: float | None = None
    for index, instant in enumerate(instants):
        position_a = interpolate_position(fixes_a, instant, max_gap_s=settings.max_gap_s)
        position_b = interpolate_position(fixes_b, instant, max_gap_s=settings.max_gap_s)
        if position_a is None or position_b is None:
            closeness_at.append(None)
            continue
        distance_m, closeness = pair_closeness(position_a, position_b, params=settings)
        closeness_at.append(closeness)
        if index == 0:
            distance_now_m = distance_m

    windows = tuple(_window_estimate(spec, closeness_at, settings) for spec in settings.windows)
    recent = windows[0]
    if not recent.known:
        not_close = _apart_by_no_recent_closeness((track_a, track_b), cleaned, now=now, params=settings)
        if not_close is None:
            return unknown(UnknownReason.TOO_LITTLE_COVERAGE, None, windows)
        return _apart_estimate(
            ApartBasis.NOT_RECENTLY_CLOSE, not_close, now=now, quality=(dropped_quality, dropped_speed)
        )
    known = [window for window in windows if window.closeness is not None]
    total_weight = sum(window.weight for window in known)
    confidence = sum(window.weight * (window.closeness or 0.0) for window in known) / total_weight
    return TogetherEstimate(
        anchor=anchor,
        anchor_lag_s=max(0.0, now - anchor),
        apart_basis=None,
        apart_for_s=_apart_for_s(closeness_at, settings),
        confidence=min(1.0, max(0.0, confidence)),
        distance_now_m=distance_now_m,
        dropped_quality=dropped_quality,
        dropped_speed=dropped_speed,
        newest_fix_age_s=ages,
        recent_closeness=recent.closeness,
        together_for_s=_together_for_s(closeness_at, settings),
        unknown_reason=None,
        unknown_track=None,
        windows=windows,
    )


def interpolate_position(
    track: Sequence[TrackFix],
    at: float,
    *,
    max_gap_s: float,
) -> InterpolatedPosition | None:
    """Estimate a position at ``at`` between the two fixes that bracket it, or ``None``.

    ``track`` must be oldest first. There is no extrapolation: an instant before
    the first or after the last fix is ``None``, as is an instant whose two
    bracketing fixes are more than ``max_gap_s`` apart.
    """
    if not track or at < track[0].at or at > track[-1].at:
        return None
    times = [fix.at for fix in track]
    upper = bisect.bisect_left(times, at)
    if times[upper] == at:
        exact = track[upper]
        return InterpolatedPosition(accuracy_m=exact.accuracy_m or 0.0, lat=exact.lat, lon=exact.lon)
    before, after = track[upper - 1], track[upper]
    if after.at - before.at > max_gap_s:
        return None
    fraction = (at - before.at) / (after.at - before.at)
    return InterpolatedPosition(
        accuracy_m=max(before.accuracy_m or 0.0, after.accuracy_m or 0.0),
        lat=before.lat + (after.lat - before.lat) * fraction,
        lon=_wrap_degrees(before.lon + _wrap_degrees(after.lon - before.lon) * fraction),
    )


def pair_closeness(
    position_a: InterpolatedPosition,
    position_b: InterpolatedPosition,
    *,
    params: TogetherParams,
) -> tuple[float, float]:
    """Return ``(distance_m, closeness)``: 1.0 within ``near_m`` plus both accuracies, 0.0 from ``far_m``."""
    distance_m = _haversine_m(position_a.lat, position_a.lon, position_b.lat, position_b.lon)
    slack_m = position_a.accuracy_m + position_b.accuracy_m
    near_m = params.near_m + slack_m
    far_m = params.far_m + slack_m
    if distance_m <= near_m:
        return distance_m, 1.0
    if distance_m >= far_m:
        return distance_m, 0.0
    return distance_m, (far_m - distance_m) / (far_m - near_m)


_COMPARE_TOLERANCE: Final[float] = 1e-9
_EARTH_RADIUS_M: Final[float] = 6_371_000.0
_HOME_WIFI_REPORT_FRESH_S: Final[float] = 1200.0
_STATIONARY_REPORT_FRESH_S: Final[float] = 1200.0
_TOGETHER_RUN_CLOSENESS: Final[float] = 0.5


def _apart_by_coarse_fixes(
    tracks: tuple[Sequence[TrackFix], Sequence[TrackFix]],
    cleaned: tuple[CleanTrack, CleanTrack],
    *,
    now: float,
    params: TogetherParams,
) -> tuple[TrackFix, TrackFix, float] | None:
    """Return both newest fixes and their distance when they are clearly apart whatever their quality, else ``None``.

    "Clearly" means the distance beyond both accuracy circles and the ground a car
    could have covered since each fix is still at least ``far_m``. A coarse fix near
    the other user, or an old one that a car could have driven in from, stays unknown.
    """
    newest = (_newest_fix(tracks[0], cleaned[0], params), _newest_fix(tracks[1], cleaned[1], params))
    first, second = newest
    if first is None or second is None:
        return None
    distance_m = _haversine_m(first.lat, first.lon, second.lat, second.lon)
    slack_m = (first.accuracy_m or 0.0) + (second.accuracy_m or 0.0)
    reach_m = params.max_speed_mps * (max(0.0, now - first.at) + max(0.0, now - second.at))
    if distance_m - slack_m - reach_m < params.far_m:
        return None
    return first, second, distance_m


def _apart_by_home_wifi(
    tracks: tuple[Sequence[TrackFix], Sequence[TrackFix]],
    cleaned: tuple[CleanTrack, CleanTrack],
    *,
    now: float,
    params: TogetherParams,
) -> tuple[TrackFix, TrackFix, float] | None:
    """Return both fixes and their distance when one user is at home on the Wi-Fi and the other is clearly away.

    The user at home is the one whose newest report, at most ``_HOME_WIFI_REPORT_FRESH_S`` old, was made on the
    home network (a repeated old fix still counts: the report says where the phone is). The other user needs a
    fresh good fix that is not on the home network, and the distance beyond both accuracy circles must be at least
    ``far_m``, and the other user's track must not have been near the reported position (they may have left it
    together). Nobody arrives in the same vehicle as a person who is already home.
    """
    for home in (0, 1):
        reports = [
            fix
            for fix in tracks[home]
            if fix.reported_at is not None
            and math.isfinite(fix.reported_at)
            and fix.reported_at <= now
            and _has_position(fix)
        ]
        if not reports:
            continue
        newest = max(reports, key=lambda fix: fix.reported_at or 0.0)
        if not newest.home_wifi or now - (newest.reported_at or 0.0) > _HOME_WIFI_REPORT_FRESH_S:
            continue
        if newest.accuracy_m is None or not math.isfinite(newest.accuracy_m) or newest.accuracy_m < 0.0:
            # Without a usable accuracy the cached position proves no separation (a missing value is not zero).
            continue
        away = _newest_fix(tracks[1 - home], cleaned[1 - home], params)
        if away is None or away.home_wifi or now - away.at > params.freshness_s:
            continue
        distance_m = _haversine_m(newest.lat, newest.lon, away.lat, away.lon)
        slack_m = newest.accuracy_m + (away.accuracy_m or 0.0)
        if distance_m - slack_m < params.far_m:
            continue
        if any(
            _haversine_m(newest.lat, newest.lon, fix.lat, fix.lon) - newest.accuracy_m - (fix.accuracy_m or 0.0)
            < params.far_m
            for fix in cleaned[1 - home].fixes
        ):
            # The other user was recently at the reported position, so the two may have left it together.
            continue
        return (newest, away, distance_m) if home == 0 else (away, newest, distance_m)
    return None


def _apart_by_no_recent_closeness(
    tracks: tuple[Sequence[TrackFix], Sequence[TrackFix]],
    cleaned: tuple[CleanTrack, CleanTrack],
    *,
    now: float,
    params: TogetherParams,
) -> tuple[TrackFix, TrackFix, float] | None:
    """Return both newest fixes and their distance when the users were never close in the last few minutes.

    Each instant of the last ``recent_close_window_s`` takes a user's interpolated position, or their newest good
    fix at or before it (a phone that stopped reporting is assumed to stay where it was). Any instant with some
    closeness is evidence the pair may still be together, so the estimate stays unknown. A user without a good fix
    has no position to compare, so that stays unknown too. A newer fix that quality filtering dropped is still a
    sign of where its user is: when its circle reaches within ``far_m`` of the other user, or it has no usable
    accuracy to rule that out, the pair stays unknown.
    """
    first, second = cleaned[0].fixes, cleaned[1].fixes
    if not first or not second:
        return None
    if _newer_dropped_fix_may_be_close(tracks, cleaned, now=now, params=params):
        return None
    for index in range(int(params.recent_close_window_s // params.step_s) + 1):
        instant = now - index * params.step_s
        position_a = _position_at(first, instant)
        position_b = _position_at(second, instant)
        if position_a is None or position_b is None:
            continue
        if pair_closeness(position_a, position_b, params=params)[1] > 0.0:
            return None
    newest_a, newest_b = first[-1], second[-1]
    return newest_a, newest_b, _haversine_m(newest_a.lat, newest_a.lon, newest_b.lat, newest_b.lon)


def _apart_by_stationary_peer(
    tracks: tuple[Sequence[TrackFix], Sequence[TrackFix]],
    cleaned: tuple[CleanTrack, CleanTrack],
    *,
    stale: int,
    now: float,
    params: TogetherParams,
) -> tuple[TrackFix, TrackFix, float] | None:
    """Return both newest fixes and their distance when the stale user has not moved and is clearly far away.

    The stale user counts as stationary when at least two reports in the last
    ``STATIONARY_REPORT_WINDOW_S`` carry the same fix and the newest of them is at
    most ``_STATIONARY_REPORT_FRESH_S`` old: the phone is alive and its position
    has not changed. The other user must have a fresh fix, and the distance beyond
    both accuracy circles (and the ground the other user could have covered since
    their fix) must still be at least ``far_m``. The stationary user's own reach
    is zero.
    """
    other = 1 - stale
    if not cleaned[stale].fixes:
        return None
    fixed = cleaned[stale].fixes[-1]
    peer = _newest_fix(tracks[other], cleaned[other], params)
    if peer is None or now - peer.at > params.freshness_s:
        return None
    if any(
        _has_position(fix)
        and (fix.at > fixed.at or (fix.at == fixed.at and (fix.lat, fix.lon) != (fixed.lat, fixed.lon)))
        for fix in tracks[stale]
    ):
        # A newer location of any quality (even one the accuracy filter dropped), or the same time at another
        # position, contradicts the cached fix, so the repeated report no longer says the phone has not moved.
        return None
    matching = [
        fix
        for fix in tracks[stale]
        if fix.reported_at is not None
        and math.isfinite(fix.reported_at)
        and (fix.at, fix.lat, fix.lon) == (fixed.at, fixed.lat, fixed.lon)
        and 0.0 <= now - fix.reported_at <= STATIONARY_REPORT_WINDOW_S
    ]
    # Distinct report times: one delivery stored twice is still one observation.
    reports = {fix.reported_at for fix in matching if fix.reported_at is not None}
    if len(reports) < 2 or now - max(reports) > _STATIONARY_REPORT_FRESH_S:
        return None
    first_report = min(reports)
    if any(
        _has_position(fix)
        and fix.reported_at is not None
        and fix.reported_at > first_report
        and (fix.lat, fix.lon) != (fixed.lat, fixed.lon)
        for fix in tracks[stale]
    ):
        # A report at another position after the repeated ones began, whatever its fix time or accuracy, says the
        # phone is not where the cached fix puts it.
        return None
    accuracies = [fix.accuracy_m for fix in matching]
    if any(accuracy is None or not math.isfinite(accuracy) or accuracy < 0.0 for accuracy in accuracies):
        return None
    distance_m = _haversine_m(fixed.lat, fixed.lon, peer.lat, peer.lon)
    slack_m = max(accuracy for accuracy in accuracies if accuracy is not None) + (peer.accuracy_m or 0.0)
    reach_m = params.max_speed_mps * max(0.0, now - peer.at)
    if distance_m - slack_m - reach_m < params.far_m:
        return None
    return (fixed, peer, distance_m) if stale == 0 else (peer, fixed, distance_m)


def _apart_estimate(
    basis: ApartBasis,
    evidence: tuple[TrackFix, TrackFix, float],
    *,
    now: float,
    quality: tuple[tuple[int, int], tuple[int, int]],
) -> TogetherEstimate:
    """A known, clearly apart estimate that rests on the newest fix of each user rather than on the windows."""
    first, second, distance_m = evidence
    anchor = min(now, first.at, second.at)
    return TogetherEstimate(
        anchor=anchor,
        anchor_lag_s=max(0.0, now - anchor),
        apart_basis=basis,
        apart_for_s=0.0,
        confidence=0.0,
        distance_now_m=distance_m,
        dropped_quality=quality[0],
        dropped_speed=quality[1],
        newest_fix_age_s=(max(0.0, now - first.at), max(0.0, now - second.at)),
        recent_closeness=0.0,
        together_for_s=0.0,
        unknown_reason=None,
        unknown_track=None,
        windows=(),
    )


def _apart_for_s(closeness_at: Sequence[float | None], params: TogetherParams) -> float:
    """Seconds back from the anchor over which every sampled instant is known and below half closeness.

    Half closeness is the middle of the near-to-far ramp and does not depend on a rule's ``min_confidence``,
    mirroring the "together" run: it dates when a separation began, while whether the pair counts as apart
    is decided separately by ``is_recently_apart``. The result is a lower bound, in whole steps.
    """
    reach = 0
    for index, closeness in enumerate(closeness_at):
        if closeness is None or closeness >= _TOGETHER_RUN_CLOSENESS:
            break
        reach = index
    return reach * params.step_s


def _has_position(fix: TrackFix) -> bool:
    """Whether the fix has a finite time and an in-range position, whatever its accuracy."""
    return (
        math.isfinite(fix.at)
        and math.isfinite(fix.lat)
        and math.isfinite(fix.lon)
        and -90.0 <= fix.lat <= 90.0
        and -180.0 <= fix.lon <= 180.0
    )


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _is_good_quality(fix: TrackFix, params: TogetherParams) -> bool:
    if not all(math.isfinite(value) for value in (fix.at, fix.lat, fix.lon)):
        return False
    if not (-90.0 <= fix.lat <= 90.0 and -180.0 <= fix.lon <= 180.0):
        return False
    if fix.accuracy_m is None or not math.isfinite(fix.accuracy_m):
        return False
    return 0.0 <= fix.accuracy_m <= params.accuracy_limit_m


def _newer_dropped_fix_may_be_close(
    tracks: tuple[Sequence[TrackFix], Sequence[TrackFix]],
    cleaned: tuple[CleanTrack, CleanTrack],
    *,
    now: float,
    params: TogetherParams,
) -> bool:
    """Whether a fix newer than a user's newest good one, within the window, may put that user near the other."""
    window_start = now - params.recent_close_window_s
    for index in (0, 1):
        newest_good_at = cleaned[index].fixes[-1].at
        for fix in tracks[index]:
            if fix.report_only or not _has_position(fix) or not newest_good_at < fix.at <= now or fix.at < window_start:
                continue
            other = _position_at(cleaned[1 - index].fixes, fix.at)
            if other is None:
                continue
            if fix.accuracy_m is None or not math.isfinite(fix.accuracy_m) or fix.accuracy_m < 0.0:
                return True
            distance_m = _haversine_m(fix.lat, fix.lon, other.lat, other.lon)
            if distance_m - fix.accuracy_m - other.accuracy_m < params.far_m:
                return True
    return False


def _newest_fix(fixes: Sequence[TrackFix], cleaned: CleanTrack, params: TogetherParams) -> TrackFix | None:
    """The newest fix, good or coarse, with a valid position and a reported accuracy of any size.

    Good fixes come from the cleaned track, so a spike stays out; the rest come from the raw
    fixes that only failed the accuracy limit. Equal times prefer the more accurate fix, so
    the input order cannot change the verdict. A ``report_only`` fix is evidence of a report,
    never a current position.
    """
    coarse = [
        fix
        for fix in fixes
        if not fix.report_only
        and not _is_good_quality(fix, params)
        and _has_position(fix)
        and fix.accuracy_m is not None
        and math.isfinite(fix.accuracy_m)
        and fix.accuracy_m >= 0.0
    ]
    return max((*cleaned.fixes, *coarse), key=lambda fix: (fix.at, -(fix.accuracy_m or 0.0)), default=None)


def _position_at(fixes: Sequence[TrackFix], instant: float) -> InterpolatedPosition | None:
    """The position at ``instant``: interpolated between its bracketing fixes, else the newest fix once it has passed.

    A gap between two fixes is bridged however long it is (two phones in one moving car reporting once a minute
    still track each other), while a phone that stopped reporting is assumed to stay where it was. An instant
    before the first fix has no position.
    """
    between = interpolate_position(fixes, instant, max_gap_s=math.inf)
    if between is not None:
        return between
    if not fixes or instant < fixes[0].at:
        return None
    fix = fixes[-1]
    return InterpolatedPosition(accuracy_m=fix.accuracy_m or 0.0, lat=fix.lat, lon=fix.lon)


def _speed_mps(first: TrackFix, second: TrackFix) -> float:
    """Speed implied by two fixes, after discounting the distance their accuracy circles explain."""
    distance_m = _haversine_m(first.lat, first.lon, second.lat, second.lon)
    explained_m = (first.accuracy_m or 0.0) + (second.accuracy_m or 0.0)
    return max(0.0, distance_m - explained_m) / max(second.at - first.at, 1.0)


def _together_for_s(closeness_at: Sequence[float | None], params: TogetherParams) -> float:
    """Seconds back from the anchor over which every sampled instant is known and not clearly apart.

    The run stops at the first instant that is clearly apart (closeness below 0.5)
    or unknown, so missing data never extends it. It is capped by the longest
    window; a value equal to that cap means "at least this long".
    """
    reach = 0
    for index, closeness in enumerate(closeness_at):
        if closeness is None or closeness < _TOGETHER_RUN_CLOSENESS:
            break
        reach = index
    return reach * params.step_s


def _window_estimate(
    spec: WindowSpec,
    closeness_at: Sequence[float | None],
    params: TogetherParams,
) -> WindowEstimate:
    total = round(spec.length_s / params.step_s) + 1
    values = [value for value in closeness_at[:total] if value is not None]
    coverage = len(values) / total
    known = bool(values) and coverage >= params.coverage_floor
    return WindowEstimate(
        closeness=sum(values) / len(values) if known else None,
        coverage=coverage,
        length_s=spec.length_s,
        total=total,
        usable=len(values),
        weight=spec.weight,
    )


def _without_end_spikes(fixes: list[TrackFix], max_speed_mps: float) -> list[TrackFix]:
    """Drop an end fix whose only neighbor implies an impossible speed."""
    kept = list(fixes)
    if len(kept) >= 2 and _speed_mps(kept[-2], kept[-1]) > max_speed_mps:
        kept.pop()
    if len(kept) >= 2 and _speed_mps(kept[0], kept[1]) > max_speed_mps:
        kept.pop(0)
    return kept


def _without_interior_spikes(fixes: list[TrackFix], max_speed_mps: float) -> list[TrackFix]:
    """Drop interior fixes whose speed from both neighbors is impossible, until none is left."""
    kept = list(fixes)
    changed = True
    while changed and len(kept) >= 3:
        changed = False
        for index in range(1, len(kept) - 1):
            if (
                _speed_mps(kept[index - 1], kept[index]) > max_speed_mps
                and _speed_mps(kept[index], kept[index + 1]) > max_speed_mps
            ):
                del kept[index]
                changed = True
                break
    return kept


def _wrap_degrees(value: float) -> float:
    """Wrap an angle in degrees into ``[-180, 180)`` so interpolation never goes the long way round."""
    return ((value + 180.0) % 360.0) - 180.0
