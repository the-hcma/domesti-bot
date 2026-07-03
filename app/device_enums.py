"""Shared device family and rule-action identifiers."""

from __future__ import annotations

from enum import StrEnum


class DeviceFamilyId(StrEnum):
    """Stable slug for a device manager family (UI tiles and rule actions)."""

    ANDROIDTV = "androidtv"
    KASA = "kasa"
    SONOS = "sonos"
    TAILWIND = "tailwind"
    VIZIO = "vizio"

    def display_name(self) -> str:
        """Proper-name label for user-visible errors and log messages."""
        match self:
            case DeviceFamilyId.ANDROIDTV:
                return "Google Cast"
            case DeviceFamilyId.KASA:
                return "Kasa"
            case DeviceFamilyId.SONOS:
                return "Sonos"
            case DeviceFamilyId.TAILWIND:
                return "Tailwind"
            case DeviceFamilyId.VIZIO:
                return "Vizio"


class RuleDeviceActionType(StrEnum):
    """Per-device command dispatched when an automation rule fires."""

    CLOSE = "close"
    OPEN = "open"
    PAUSE = "pause"
    RESUME = "resume"
    TURN_OFF = "turn_off"
    TURN_ON = "turn_on"

    def display_label(self) -> str:
        """Human-readable verb for user-visible errors and log messages."""
        match self:
            case RuleDeviceActionType.CLOSE:
                return "close"
            case RuleDeviceActionType.OPEN:
                return "open"
            case RuleDeviceActionType.PAUSE:
                return "pause"
            case RuleDeviceActionType.RESUME:
                return "resume"
            case RuleDeviceActionType.TURN_OFF:
                return "turn off"
            case RuleDeviceActionType.TURN_ON:
                return "turn on"


class RuleEvaluationCause(StrEnum):
    """Why a rule evaluation pass is executing."""

    DEVICE_STATE = "device_state"
    DWELL = "dwell"
    EDGE = "edge"
    ELIGIBILITY = "eligibility"
    SCHEDULED = "scheduled"


class RuleTrigger(StrEnum):
    """How a rule can be armed in automation-rules.json (``triggers`` entries)."""

    DEVICE_STATE = "device_state"
    DWELL_SATISFIED = "dwell_satisfied"
    EDGE_TRUE = "edge_true"
    SCHEDULED = "scheduled"
