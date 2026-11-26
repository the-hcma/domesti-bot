"""Human-facing timestamps: home-timezone local time with UTC in parentheses.

Every timestamp a person reads (emails, notifications, log lines, operator
output) renders as local time with its zone abbreviation, followed by the same
instant in UTC in parentheses, for example ``2026-11-25 22:58:45 EST
(2026-11-26 03:58:45 UTC)``. A bare UTC stamp hides when something happened on
the wall clock, and a bare local stamp is ambiguous across DST. Machine
interfaces (API JSON, SQLite, webhooks) keep ISO-8601 UTC and do not use this
module.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.automation_rules_loader import AutomationRulesLoadError, load_settings_location

_UTC_ZONE = ZoneInfo("UTC")

# Log lines call this on hot paths and each uncached call would re-read and
# re-validate the whole rules file, so the home timezone name is cached briefly.
# A timezone edit in settings therefore shows up in timestamps within the TTL.
_HOME_TIMEZONE_TTL_S = 30.0
_home_timezone_lock = threading.Lock()
_home_timezone_cache: tuple[float, str] | None = None
# Bumped on every clear so a load that started before a settings save cannot repopulate the cache with the old value.
_home_timezone_generation = 0


def clear_home_timezone_cache() -> None:
    """Drop the cached home timezone name (tests, and after settings are saved)."""
    global _home_timezone_cache, _home_timezone_generation  # noqa: PLW0603
    with _home_timezone_lock:
        _home_timezone_cache = None
        _home_timezone_generation += 1


def format_local_with_utc(
    when: float | datetime,
    *,
    timezone: str | ZoneInfo | None = None,
    millis: bool = False,
) -> str:
    """Render ``when`` as ``<local stamp> <zone> (<UTC stamp> UTC)``.

    ``when`` is a Unix epoch or a datetime (naive datetimes are taken as UTC).
    ``timezone`` defaults to the home timezone from ``settings_location``; an
    unreadable or invalid zone falls back to UTC so formatting never raises.
    ``millis`` appends ``.mmm`` to both stamps for sub-second gaps. When the
    local zone is UTC the instant is shown once rather than twice.
    """
    zone, zone_name = resolve_display_timezone(timezone)
    instant = _as_utc_datetime(when)
    local = instant.astimezone(zone)
    zone_label = (local.tzname() or "").strip() or zone_name
    local_stamp = f"{_stamp(local, millis=millis)} {zone_label}"
    utc_stamp = f"{_stamp(instant, millis=millis)} UTC"
    if local_stamp == utc_stamp:
        return utc_stamp
    return f"{local_stamp} ({utc_stamp})"


def resolve_display_timezone(
    timezone: str | ZoneInfo | None = None,
) -> tuple[ZoneInfo, str]:
    """Return ``(ZoneInfo, display_name)``; the home timezone when ``timezone`` is omitted.

    Falls back to UTC when the home timezone cannot be loaded or is not a valid
    IANA name, so notification formatting never fails on timezone resolution.
    """
    if isinstance(timezone, ZoneInfo):
        key = getattr(timezone, "key", None)
        return timezone, key if isinstance(key, str) and key else str(timezone)
    if isinstance(timezone, str):
        return _zoneinfo_or_utc(timezone)
    name = _home_timezone_name()
    if name is None:
        return _UTC_ZONE, "UTC"
    return _zoneinfo_or_utc(name)


def _home_timezone_name() -> str | None:
    """Return the cached home timezone name, or ``None`` when rules cannot be loaded."""
    global _home_timezone_cache  # noqa: PLW0603
    now = time.monotonic()
    with _home_timezone_lock:
        cached = _home_timezone_cache
        if cached is not None and now < cached[0]:
            return cached[1]
        generation = _home_timezone_generation
    try:
        name = load_settings_location().timezone
    except AutomationRulesLoadError:
        return None
    with _home_timezone_lock:
        if generation == _home_timezone_generation:
            _home_timezone_cache = (now + _HOME_TIMEZONE_TTL_S, name)
    return name


def _as_utc_datetime(when: float | datetime) -> datetime:
    if isinstance(when, datetime):
        return when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    return datetime.fromtimestamp(when, tz=UTC)


def _stamp(dt: datetime, *, millis: bool) -> str:
    base = dt.strftime("%Y-%m-%d %H:%M:%S")
    if not millis:
        return base
    return f"{base}.{int(dt.microsecond / 1000):03d}"


def _zoneinfo_or_utc(timezone_name: str) -> tuple[ZoneInfo, str]:
    name = timezone_name.strip()
    if not name:
        return _UTC_ZONE, "UTC"
    try:
        return ZoneInfo(name), name
    except (KeyError, ValueError):
        return _UTC_ZONE, "UTC"
