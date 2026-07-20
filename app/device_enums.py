"""Shared device family and rule-action identifiers."""

from __future__ import annotations

from enum import StrEnum


class DeviceConditionState(StrEnum):
    """Canonical cached device state vocabulary (rules, actions, device views).

    Wire values are shared across dwell conditions, action expected-state
    helpers, and per-device ``power_state`` / ``door_state`` properties.

    The web UI may also report ``\"unknown\"`` for transient readings; that
    value is UI-only and is not a member of this enum.

    ``OCCUPIED`` / ``CLEAR`` are room-occupancy vocabulary for the EP1 family
    (Everything Presence One). They are distinct from My Tracks presence /
    user / location terms.
    """

    CLEAR = "clear"
    CLOSED = "closed"
    OCCUPIED = "occupied"
    OFF = "off"
    ON = "on"
    OPEN = "open"
    PAUSED = "paused"
    PLAYING = "playing"

    def desired_bool(self) -> bool:
        """Return the natural cached bool that means this state is currently true."""
        return self in (
            DeviceConditionState.OCCUPIED,
            DeviceConditionState.ON,
            DeviceConditionState.OPEN,
            DeviceConditionState.PLAYING,
        )

    def supported_by_family(self, family_id: DeviceFamilyId) -> bool:
        """Return whether ``family_id`` can report this state from cached readings."""
        match self:
            case DeviceConditionState.CLEAR | DeviceConditionState.OCCUPIED:
                return family_id == DeviceFamilyId.EP1
            case DeviceConditionState.OPEN | DeviceConditionState.CLOSED:
                return family_id == DeviceFamilyId.TAILWIND
            case DeviceConditionState.PLAYING | DeviceConditionState.PAUSED:
                return family_id == DeviceFamilyId.SONOS
            case DeviceConditionState.ON | DeviceConditionState.OFF:
                return family_id in (
                    DeviceFamilyId.KASA,
                    DeviceFamilyId.SONOS,
                    DeviceFamilyId.VIZIO,
                )


class DeviceFamilyId(StrEnum):
    """Stable slug for a device manager family (UI tiles and rule actions)."""

    ANDROIDTV = "androidtv"
    EP1 = "ep1"
    KASA = "kasa"
    SONOS = "sonos"
    TAILWIND = "tailwind"
    VIZIO = "vizio"

    def display_name(self) -> str:
        """Proper-name label for user-visible errors and log messages."""
        match self:
            case DeviceFamilyId.ANDROIDTV:
                return "Google Cast"
            case DeviceFamilyId.EP1:
                return EP1_DISPLAY_NAME
            case DeviceFamilyId.KASA:
                return "Kasa"
            case DeviceFamilyId.SONOS:
                return "Sonos"
            case DeviceFamilyId.TAILWIND:
                return "Tailwind"
            case DeviceFamilyId.VIZIO:
                return "Vizio"


class DeviceIdResolution(StrEnum):
    """How automation-rule ``device_id`` values are interpreted on disk."""

    MAC = "mac"
    PREFERRED_LABEL = "preferred_label"


EP1_DISPLAY_NAME = "Everything Presence One"


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


class SettingsCredentialsTestSource(StrEnum):
    """Where credentials used by a Settings Test probe were resolved from."""

    CLI = "cli"
    DATABASE = "database"
    ENV = "env"
    FORM = "form"


class UiActionType(StrEnum):
    """Web UI device command logged via ``[ui-action]`` lines."""

    BULK_OFF = "bulk_off"
    CLOSE = "close"
    CLOSE_ALL = "close_all"
    OPEN = "open"
    PAUSE_ALL = "pause_all"
    TOGGLE = "toggle"


class VacationEmailSource(StrEnum):
    """What triggered a vacation-mode notification email."""

    ANOMALY = "anomaly"
    LATCH = "latch"
    SETTINGS_TEST = "settings_test"


class VacationModeTestEmailKind(StrEnum):
    """Sample email kinds for ``POST /v1/rules/settings/vacation-mode/test``."""

    ANOMALY = "anomaly"
    ARM = "arm"
    DISARM = "disarm"
