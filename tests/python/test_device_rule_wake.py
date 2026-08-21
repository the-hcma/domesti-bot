"""Hermetic tests for device rule-wake notification."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from app.device_enums import DeviceFamilyId, Ep1ReadingMetric
from app.device_rule_wake import DeviceRuleWakeNotifier
from app.rule_evaluator import RuleEvaluator
from app.server_runtime import DomestiServerRuntime


def test_device_rule_wake_notifier_ignores_first_sample() -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    notifier.note_bool_transition(DeviceFamilyId.TAILWIND, "Left", False)
    on_change.assert_not_called()


def test_device_rule_wake_notifier_note_reading_invokes_every_sample() -> None:
    on_change = MagicMock()
    on_reading = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change, on_reading=on_reading)
    notifier.note_reading(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    notifier.note_reading(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    assert on_reading.call_count == 2
    on_reading.assert_called_with(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    on_change.assert_not_called()


def test_device_rule_wake_notifier_note_reading_noop_without_callback() -> None:
    on_change = MagicMock()
    notifier = DeviceRuleWakeNotifier(on_change)
    notifier.note_reading(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.TEMPERATURE_C,
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
    notifier.note_reading(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    schedule_spy.assert_called_once_with(
        DeviceFamilyId.EP1,
        "aa:bb:cc:dd:ee:01",
        reading_metric=Ep1ReadingMetric.ILLUMINANCE_LX,
    )
    holder.schedule_vacation_anomaly_alert.assert_not_called()
