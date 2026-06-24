"""Tests for in-memory operator alert store."""

from __future__ import annotations

from app.operator_alerts import operator_alert_store


def test_record_and_clear_smtp_notification_failure() -> None:
    operator_alert_store.clear_smtp_notification_failure()
    alert = operator_alert_store.record_smtp_notification_failure(
        message="Connection refused",
        reason_code="smtp_delivery_failed",
    )
    current = operator_alert_store.current_smtp_notification_failure()
    assert current is not None
    assert current.message == "Connection refused"
    assert current.reason_code == "smtp_delivery_failed"
    assert current.recorded_at == alert.recorded_at

    operator_alert_store.clear_smtp_notification_failure()
    assert operator_alert_store.current_smtp_notification_failure() is None
