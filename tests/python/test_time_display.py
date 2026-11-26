from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.automation_rules_loader import AutomationRulesLoadError
from app.time_display import clear_home_timezone_cache, format_local_with_utc

_EPOCH = 1_700_000_000.0  # 2023-11-14 22:13:20 UTC
_NEW_YORK = ZoneInfo("America/New_York")


def test_local_time_with_utc_in_parentheses() -> None:
    assert format_local_with_utc(_EPOCH, timezone=_NEW_YORK) == "2023-11-14 17:13:20 EST (2023-11-14 22:13:20 UTC)"


def test_daylight_saving_abbreviation_and_date_rollover() -> None:
    when = datetime(2026, 11, 26, 3, 58, 45, tzinfo=UTC)
    assert (
        format_local_with_utc(when, timezone="America/New_York") == "2026-11-25 22:58:45 EST (2026-11-26 03:58:45 UTC)"
    )
    summer = datetime(2026, 7, 15, 18, 30, tzinfo=UTC)
    assert (
        format_local_with_utc(summer, timezone="America/New_York")
        == "2026-07-15 14:30:00 EDT (2026-07-15 18:30:00 UTC)"
    )


def test_millis_apply_to_both_stamps() -> None:
    assert (
        format_local_with_utc(_EPOCH + 0.158, timezone="America/New_York", millis=True)
        == "2023-11-14 17:13:20.158 EST (2023-11-14 22:13:20.158 UTC)"
    )


def test_naive_datetime_is_taken_as_utc() -> None:
    assert (
        format_local_with_utc(datetime(2026, 1, 2, 3, 4, 5), timezone="America/New_York")  # noqa: DTZ001
        == "2026-01-01 22:04:05 EST (2026-01-02 03:04:05 UTC)"
    )


def test_utc_home_zone_shows_the_instant_once() -> None:
    assert format_local_with_utc(_EPOCH, timezone="UTC") == "2023-11-14 22:13:20 UTC"


def test_invalid_zone_falls_back_to_utc() -> None:
    assert format_local_with_utc(_EPOCH, timezone="Not/A_Real_Zone") == "2023-11-14 22:13:20 UTC"


def test_omitted_zone_uses_home_timezone_from_settings() -> None:
    class _Loc:
        timezone = "America/New_York"

    with patch("app.time_display.load_settings_location", return_value=_Loc()):
        assert format_local_with_utc(_EPOCH) == "2023-11-14 17:13:20 EST (2023-11-14 22:13:20 UTC)"


def test_missing_rules_file_falls_back_to_utc() -> None:
    with patch(
        "app.time_display.load_settings_location",
        side_effect=AutomationRulesLoadError("missing"),
    ):
        assert format_local_with_utc(_EPOCH) == "2023-11-14 22:13:20 UTC"


def test_home_timezone_is_cached_so_hot_log_paths_do_not_reread_the_rules_file() -> None:
    class _Loc:
        timezone = "America/New_York"

    with patch("app.time_display.load_settings_location", return_value=_Loc()) as loader:
        for _ in range(5):
            format_local_with_utc(_EPOCH)
    assert loader.call_count == 1


def test_clearing_the_cache_picks_up_a_changed_timezone() -> None:
    class _Ny:
        timezone = "America/New_York"

    class _La:
        timezone = "America/Los_Angeles"

    with patch("app.time_display.load_settings_location", return_value=_Ny()):
        assert "EST" in format_local_with_utc(_EPOCH)
    clear_home_timezone_cache()
    with patch("app.time_display.load_settings_location", return_value=_La()):
        assert "PST" in format_local_with_utc(_EPOCH)


def test_a_failed_rules_load_is_not_cached() -> None:
    class _Loc:
        timezone = "America/New_York"

    with patch("app.time_display.load_settings_location", side_effect=AutomationRulesLoadError("x")):
        assert format_local_with_utc(_EPOCH) == "2023-11-14 22:13:20 UTC"
    with patch("app.time_display.load_settings_location", return_value=_Loc()):
        assert "EST" in format_local_with_utc(_EPOCH)


def test_a_load_that_raced_a_cache_clear_does_not_repopulate_the_cache() -> None:
    class _Old:
        timezone = "America/New_York"

    class _New:
        timezone = "America/Los_Angeles"

    def _slow_old_load() -> _Old:
        # A settings save lands (and clears the cache) while this load is in flight.
        clear_home_timezone_cache()
        return _Old()

    with patch("app.time_display.load_settings_location", side_effect=_slow_old_load):
        assert "EST" in format_local_with_utc(_EPOCH)
    with patch("app.time_display.load_settings_location", return_value=_New()):
        assert "PST" in format_local_with_utc(_EPOCH)
