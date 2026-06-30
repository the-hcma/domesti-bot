"""Report vs fix time for user location rows.

Webhook ``timestamp`` is the GPS fix (OwnTracks ``tst``). ``reported_at`` and related
metadata reflect when the device report was built or ingested. Ordering, staleness, and
UI "last heard" use report time; geography and stale-position notes use fix time.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from app.api.schemas import LocationUpdateWebhookIn
from app.logging_config import format_log_timestamp

FIX_AGE_LABEL_THRESHOLD_SECONDS = 60


def format_duration_short(total_seconds: int) -> str:
    """Compact duration for logs (e.g. ``1h 52m``)."""
    seconds = max(0, int(total_seconds))
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    parts: list[str] = []
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    if hours == 0 and (minutes == 0 or secs > 0):
        parts.append(f"{secs}s")
    return " ".join(parts) or "0s"


def location_epoch_to_iso_z(epoch: float) -> str:
    """Format a location epoch as a UTC ISO-8601 string with a ``Z`` suffix."""
    return datetime.fromtimestamp(epoch, tz=UTC).isoformat().replace("+00:00", "Z")


def location_fix_age_seconds(*, reported_at: float, fix_at: float) -> int:
    """Seconds between GPS fix time and report time (0 when fix is newer than report)."""
    return max(0, int(reported_at - fix_at))


def location_fix_at_epoch_from_webhook(body: LocationUpdateWebhookIn) -> float:
    """Parse GPS fix time from webhook ``timestamp`` (OwnTracks ``tst``)."""
    return parse_iso_timestamp_to_epoch(body.timestamp)


def location_report_log_fragment(*, reported_at: float, fix_at: float) -> str:
    """Space-prefixed report vs fix times for stored-location log lines."""
    return (
        f" report_at={format_log_timestamp(reported_at)}"
        f" fix_at={format_log_timestamp(fix_at)}"
    )


def location_report_log_stale_suffix(
    *,
    reported_at: float,
    fix_at: float,
    trigger: str | None,
) -> str:
    """Optional trigger and fix-age context when fix predates report materially."""
    fix_age_s = location_fix_age_seconds(reported_at=reported_at, fix_at=fix_at)
    if fix_age_s < FIX_AGE_LABEL_THRESHOLD_SECONDS:
        return ""
    parts = [f"fix_was={format_duration_short(fix_age_s)}_before_report"]
    if trigger is not None and trigger.strip() != "":
        parts.append(f"trigger={trigger!r}")
    return " " + " ".join(parts)


def location_reported_at_epoch_from_webhook(
    body: LocationUpdateWebhookIn,
    *,
    ingest_at: float | None = None,
) -> float:
    """Resolve report time from webhook top-level and metadata fields."""
    for raw in (body.reported_at, body.owntracks_created_at, body.received_at):
        if raw is not None and raw.strip() != "":
            return parse_iso_timestamp_to_epoch(raw)
    if ingest_at is not None:
        return ingest_at
    return time.time()


def parse_iso_timestamp_to_epoch(raw: str) -> float:
    """Parse an ISO-8601 timestamp from My Tracks export JSON or webhooks."""
    normalized = raw.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError as exc:
        raise ValueError(f"Expected ISO-8601 timestamp, got {raw!r}") from exc
