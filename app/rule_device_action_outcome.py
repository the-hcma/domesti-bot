"""Shared types for rule device action dispatch outcomes."""

from __future__ import annotations

from dataclasses import dataclass

from app.device_enums import DeviceFamilyId, RuleDeviceActionType


@dataclass(frozen=True)
class RuleDeviceActionOutcome:
    """Observed device state before and after one dispatched rule action."""

    action: RuleDeviceActionType
    after_state: str | None
    before_state: str | None
    device_id: str
    error: str | None
    family_id: DeviceFamilyId
    probable: bool
    succeeded: bool
