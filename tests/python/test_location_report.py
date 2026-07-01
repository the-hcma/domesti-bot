"""Tests for report vs fix time helpers."""

from __future__ import annotations

from app.api.schemas import LocationUpdateWebhookIn
from app.location_report import (
    location_fix_age_seconds,
    location_fix_at_epoch_from_webhook,
    location_report_log_fragment,
    location_report_log_stale_suffix,
    location_reported_at_epoch_from_webhook,
)


def test_location_reported_at_prefers_top_level_reported_at() -> None:
    body = LocationUpdateWebhookIn.model_validate(
        {
            "user_id": "henrique",
            "lat": 41.0,
            "lon": -73.0,
            "timestamp": "2026-06-30T12:09:00+00:00",
            "reported_at": "2026-06-30T14:01:00+00:00",
        }
    )
    fix_at = location_fix_at_epoch_from_webhook(body)
    reported_at = location_reported_at_epoch_from_webhook(body)
    assert fix_at < reported_at
    assert location_fix_age_seconds(reported_at=reported_at, fix_at=fix_at) == 6720


def test_location_reported_at_falls_back_to_owntracks_created_at() -> None:
    body = LocationUpdateWebhookIn.model_validate(
        {
            "user_id": "henrique",
            "lat": 41.0,
            "lon": -73.0,
            "timestamp": "2026-06-30T12:09:00+00:00",
            "owntracks_created_at": "2026-06-30T14:01:00+00:00",
        }
    )
    reported_at = location_reported_at_epoch_from_webhook(body)
    fix_at = location_fix_at_epoch_from_webhook(body)
    assert reported_at > fix_at


def test_location_report_log_fragment_includes_both_times() -> None:
    fragment = location_report_log_fragment(
        reported_at=1_719_000_000.0,
        fix_at=1_718_993_280.0,
    )
    assert "report_at=" in fragment
    assert "fix_at=" in fragment


def test_location_report_log_stale_suffix_for_old_fix_ping() -> None:
    suffix = location_report_log_stale_suffix(
        reported_at=1_719_000_000.0,
        fix_at=1_718_993_280.0,
        trigger="p",
    )
    assert "fix_was=" in suffix
    assert "trigger='p'" in suffix
