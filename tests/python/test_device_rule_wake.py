"""Hermetic tests for device rule-wake notification."""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.device_enums import DeviceFamilyId, Ep1ReadingMetric
from app.device_rule_wake import (
    DEVICE_BOOL_METRIC_NAME,
    DEVICE_STATE_TRANSITION_LOG,
    DeviceRuleWakeNotifier,
    format_bool_state_label,
)
from app.rule_evaluator import (
    RULE_DWELL_STREAK_LOG,
    RULE_DWELL_STREAK_RESET_LOG,
    RuleEvaluator,
)
from app.server_runtime import DomestiServerRuntime


def test_format_bool_state_label_by_family() -> None:
    assert format_bool_state_label(DeviceFamilyId.EP1, True) == "occupied"
    assert format_bool_state_label(DeviceFamilyId.EP1, False) == "clear"
    assert format_bool_state_label(DeviceFamilyId.TAILWIND, True) == "open"
    assert format_bool_state_label(DeviceFamilyId.TAILWIND, False) == "closed"
    assert format_bool_state_label(DeviceFamilyId.SONOS, True) == "playing"
    assert format_bool_state_label(DeviceFamilyId.SONOS, False) == "paused"
    assert format_bool_state_label(DeviceFamilyId.KASA, True) == "on"
    assert format_bool_state_label(DeviceFamilyId.KASA, False) == "off"
    assert format_bool_state_label(DeviceFamilyId.KASA, None) == "unknown"


def test_device_rule_wake_notifier_logs_bool_transition(
    caplog: pytest.LogCaptureFixture,
) -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    with caplog.at_level(logging.INFO, logger="app.device_rule_wake"):
        assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", True) is False
        assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", False) is True
    assert (
        DEVICE_STATE_TRANSITION_LOG
        % (
            "ep1",
            "aa:bb:cc:dd:ee:01",
            DEVICE_BOOL_METRIC_NAME,
            "occupied",
            "clear",
        )
        in caplog.text
    )
    on_change.assert_called_once_with(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        True,
        False,
    )


def test_device_rule_wake_notifier_ignores_first_sample_without_seed() -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    notifier.note_bool_transition(DeviceFamilyId.TAILWIND, "Left", False)
    on_change.assert_not_called()


def test_device_rule_wake_notifier_note_reading_diffs_values() -> None:
    on_change = MagicMock()
    on_reading = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change, on_reading=on_reading)
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.ILLUMINANCE_LX,
            10.0,
        )
        is False
    )
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.ILLUMINANCE_LX,
            10.0,
        )
        is False
    )
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.ILLUMINANCE_LX,
            12.0,
        )
        is True
    )
    on_reading.assert_called_once_with(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    on_change.assert_not_called()


def test_device_rule_wake_notifier_note_reading_noop_without_callback() -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.TEMPERATURE_C,
            21.0,
        )
        is False
    )
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.TEMPERATURE_C,
            22.0,
        )
        is False
    )
    on_change.assert_not_called()


def test_device_rule_wake_notifier_notifies_on_transition() -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    assert notifier.note_bool_transition(DeviceFamilyId.TAILWIND, "Left", False) is False
    assert notifier.note_bool_transition(DeviceFamilyId.TAILWIND, "Left", True) is True
    on_change.assert_called_once_with(
        DeviceFamilyId.TAILWIND,
        "Left",
        False,
        True,
    )


def test_device_rule_wake_notifier_seeds_first_bool_sample() -> None:
    on_change = MagicMock()
    on_seed = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change, on_bool_seed=on_seed)
    assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", True) is True
    on_seed.assert_called_once_with(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    on_change.assert_not_called()
    assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", True) is False
    on_seed.assert_called_once()


def test_device_rule_wake_notifier_seeds_when_prior_was_none() -> None:
    on_change = MagicMock()
    on_seed = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change, on_bool_seed=on_seed)
    assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", None) is False
    on_seed.assert_not_called()
    assert notifier.note_bool_transition(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01", True) is True
    on_seed.assert_called_once_with(DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    on_change.assert_not_called()


def test_runtime_reading_update_schedules_rule_evaluation_not_vacation(
    tmp_path: Path,
) -> None:
    """Reading wakes forward through runtime → RuleEvaluator.schedule_device_state_change."""
    db = tmp_path / "discovery.sqlite"
    db.touch()
    holder = DomestiServerRuntime()
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: 1_700_000_000.0,
    )
    holder.rule_evaluator = evaluator
    holder.schedule_vacation_anomaly_alert = MagicMock()  # type: ignore[method-assign]
    schedule_spy = MagicMock(wraps=evaluator.schedule_device_state_change)
    evaluator.schedule_device_state_change = schedule_spy  # type: ignore[method-assign]
    notifier = holder.build_device_rule_wake_notifier()
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.ILLUMINANCE_LX,
            10.0,
        )
        is False
    )
    schedule_spy.assert_not_called()
    assert (
        notifier.note_reading(
            DeviceFamilyId.EP1,
            "aa:bb:cc:dd:ee:01",
            Ep1ReadingMetric.ILLUMINANCE_LX,
            11.0,
        )
        is True
    )
    schedule_spy.assert_called_once_with(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        reading_metric=Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    holder.schedule_vacation_anomaly_alert.assert_not_called()


def test_device_bool_streak_logs_start_and_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = tmp_path / "discovery.sqlite"
    db.touch()
    since = 1_700_000_000.0
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: None,
        now_fn=lambda: since,
    )
    key = (DeviceFamilyId.EP1, "aa:bb:cc:dd:ee:01")
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        evaluator._set_device_bool_streak(key, False, since)
        evaluator._drop_device_bool_streak(key)
    assert (
        RULE_DWELL_STREAK_LOG
        % (
            "ep1",
            "aa:bb:cc:dd:ee:01",
            "clear",
            "2023-11-14T22:13:20Z",
        )
        in caplog.text
    )
    assert (
        RULE_DWELL_STREAK_RESET_LOG
        % (
            "ep1",
            "aa:bb:cc:dd:ee:01",
            "clear",
            "unknown",
        )
        in caplog.text
    )
