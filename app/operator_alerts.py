"""In-memory operator alerts surfaced in the web UI."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass(frozen=True)
class OperatorAlert:
    """A single operator-facing alert with a stable reason code."""

    message: str
    reason_code: str
    recorded_at: float


class OperatorAlertStore:
    """Process-local holder for dismissible operator alerts."""

    def __init__(self) -> None:
        self._smtp_notification_failure: OperatorAlert | None = None

    def clear_smtp_notification_failure(self) -> None:
        self._smtp_notification_failure = None

    def current_smtp_notification_failure(self) -> OperatorAlert | None:
        return self._smtp_notification_failure

    def record_smtp_notification_failure(
        self,
        *,
        message: str,
        reason_code: str = "smtp_delivery_failed",
    ) -> OperatorAlert:
        alert = OperatorAlert(
            message=message,
            reason_code=reason_code,
            recorded_at=time.time(),
        )
        self._smtp_notification_failure = alert
        return alert


operator_alert_store = OperatorAlertStore()
