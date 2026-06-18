"""Hermetic tests for cron schedule helpers."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.cron_schedule import next_scheduled_evaluate_at, validate_schedule_cron_expression


def test_validate_schedule_cron_expression_accepts_five_field_cron() -> None:
    validate_schedule_cron_expression("*/15 * * * *")


def test_validate_schedule_cron_expression_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        validate_schedule_cron_expression("")


def test_validate_schedule_cron_expression_rejects_invalid() -> None:
    with pytest.raises(ValueError, match="5-field cron"):
        validate_schedule_cron_expression("not a cron")


def test_validate_schedule_cron_expression_rejects_six_field_cron() -> None:
    with pytest.raises(ValueError, match="5-field cron"):
        validate_schedule_cron_expression("0 0 1 1 1 2020")


def test_next_scheduled_evaluate_at_returns_now_when_boundary_matches() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=tz)
    next_at = next_scheduled_evaluate_at("0 12 * * *", now, tz, due_if_matching=True)
    assert next_at == now.timestamp()


def test_next_scheduled_evaluate_at_returns_future_epoch() -> None:
    tz = ZoneInfo("America/New_York")
    now = datetime(2024, 6, 15, 12, 7, 0, tzinfo=tz)
    next_at = next_scheduled_evaluate_at("*/15 * * * *", now, tz)
    assert next_at > now.timestamp()
