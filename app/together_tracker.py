"""Remember when pairs of users start and stop travelling together, to leave breadcrumbs for auditing.

The ``users_together`` condition is a stateless check that runs on every
evaluation, so it cannot tell a new togetherness from a continuing one. The
tracker keeps the previous answer per pair and turns a stream of estimates into
at most one event per observation:

* an episode **begins** when the pair is together (the same test as
  ``expected: true``), and records when the evidence says it really started;
* an episode **ends only when the data supports it**: the last minute is known
  and clearly apart (closeness at most ``1 - min_confidence``). The weighted
  5 and 10 minute windows stay high for minutes after a real separation, so
  they are not part of this test; the end time is the estimated start of the
  separation, not the moment it was noticed. The dead zone does not end an
  episode, and neither does missing or stale data; such evaluations are counted.

State is in memory. A restart forgets open episodes, so the first time a pair can
be evaluated after the start (``first_sight``) is reported as such if it is
already together, instead of as a fresh start.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.location_report import location_epoch_to_iso_z
from app.users_together import TogetherEstimate

TogetherKey = tuple[tuple[str, str], float]
"""A tracked pair (sorted user ids) and the ``min_confidence`` it is judged by."""


@dataclass(frozen=True, slots=True)
class TogetherBegan:
    """A pair was found together; ``since`` is when the evidence says the episode started."""

    closeness: tuple[float | None, ...]
    confidence: float
    distance_m: float | None
    first_sight: bool
    horizon_capped: bool
    min_confidence: float
    newest_fix_age_s: tuple[float | None, float | None]
    since: float
    users: tuple[str, str]


@dataclass(frozen=True, slots=True)
class TogetherEnded:
    """A pair that was together is now clearly apart."""

    began_at: float
    confidence: float
    detected_at: float
    distance_m: float | None
    duration_s: float
    ended_at: float
    low: float
    min_confidence: float
    peak: float
    unknown_evaluations: int
    users: tuple[str, str]


TogetherEvent = TogetherBegan | TogetherEnded


class TogetherTracker:
    """Per-pair episode state; feed it every estimate of a tracked pair with :meth:`observe`."""

    def __init__(self) -> None:
        self._episodes: dict[TogetherKey, _Episode] = {}
        self._seen: set[TogetherKey] = set()

    def observe(
        self,
        key: TogetherKey,
        estimate: TogetherEstimate,
    ) -> TogetherEvent | None:
        """Update the pair's state with ``estimate`` and return the event it caused, if any."""
        users, min_confidence = key
        episode = self._episodes.get(key)
        if estimate.confidence is None or estimate.anchor is None:
            if episode is not None:
                episode.unknown_evaluations += 1
            return None
        first_sight = key not in self._seen
        self._seen.add(key)
        if episode is not None:
            episode.peak = max(episode.peak, estimate.confidence)
            episode.low = min(episode.low, estimate.confidence)
            if estimate.is_recently_apart(min_confidence):
                del self._episodes[key]
                ended_at = max(episode.began_at, estimate.anchor - (estimate.apart_for_s or 0.0))
                return TogetherEnded(
                    began_at=episode.began_at,
                    confidence=estimate.confidence,
                    detected_at=estimate.anchor,
                    distance_m=estimate.distance_now_m,
                    duration_s=ended_at - episode.began_at,
                    ended_at=ended_at,
                    low=episode.low,
                    min_confidence=min_confidence,
                    peak=episode.peak,
                    unknown_evaluations=episode.unknown_evaluations,
                    users=users,
                )
            return None
        if not estimate.is_together(min_confidence):
            return None
        together_for_s = estimate.together_for_s or 0.0
        horizon_s = estimate.windows[-1].length_s if estimate.windows else 0.0
        since = estimate.anchor - together_for_s
        self._episodes[key] = _Episode(
            began_at=since,
            low=estimate.confidence,
            peak=estimate.confidence,
            unknown_evaluations=0,
        )
        return TogetherBegan(
            closeness=tuple(window.closeness for window in estimate.windows),
            confidence=estimate.confidence,
            distance_m=estimate.distance_now_m,
            first_sight=first_sight,
            horizon_capped=bool(horizon_s) and together_for_s >= horizon_s,
            min_confidence=min_confidence,
            newest_fix_age_s=estimate.newest_fix_age_s,
            since=since,
            users=users,
        )

    def retain(self, keys: Iterable[TogetherKey]) -> None:
        """Forget every pair that is no longer tracked (its rule was disabled or removed)."""
        wanted = set(keys)
        self._episodes = {key: episode for key, episode in self._episodes.items() if key in wanted}
        self._seen &= wanted


def format_together_event(
    event: TogetherEvent,
    *,
    names: str,
    rule_ids: Iterable[str],
) -> str:
    """One log line for ``event``; ``names`` is the human-readable pair and ``rule_ids`` the rules that track it."""
    users = ",".join(event.users)
    rules = ",".join(sorted(rule_ids))
    distance = "unknown" if event.distance_m is None else f"{event.distance_m:.0f}"
    if isinstance(event, TogetherBegan):
        windows = " ".join(
            f"closeness_{minutes}m={'n/a' if value is None else f'{value:.2f}'}"
            for minutes, value in zip((1, 5, 10), event.closeness, strict=False)
        )
        ages = ",".join("unknown" if age is None else f"{age:.0f}s" for age in event.newest_fix_age_s)
        return (
            f"[rules] together began users={users} names={names!r} since={location_epoch_to_iso_z(event.since)} "
            f"confidence={event.confidence:.2f} {windows} distance_m={distance} fix_ages={ages} "
            f"min_confidence={event.min_confidence:.2f} first_sight={str(event.first_sight).lower()} "
            f"horizon_capped={str(event.horizon_capped).lower()} rules={rules}"
        )
    return (
        f"[rules] together ended users={users} names={names!r} began={location_epoch_to_iso_z(event.began_at)} "
        f"ended={location_epoch_to_iso_z(event.ended_at)} detected={location_epoch_to_iso_z(event.detected_at)} "
        f"duration_s={event.duration_s:.0f} "
        f"peak={event.peak:.2f} low={event.low:.2f} unknown_evaluations={event.unknown_evaluations} "
        f"confidence={event.confidence:.2f} distance_m={distance} "
        f"min_confidence={event.min_confidence:.2f} reason=clearly_apart rules={rules}"
    )


@dataclass(slots=True)
class _Episode:
    began_at: float
    low: float
    peak: float
    unknown_evaluations: int
