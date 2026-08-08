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


class Ep1BluetoothProxyState(StrEnum):
    """ESPHome ``bluetooth_proxy`` select options (wire values match firmware)."""

    DISABLED = "Disabled"
    ENABLED = "Enabled"


class Ep1CalibrationOffsetKind(StrEnum):
    """ESPHome number offsets exposed under Settings → EP1 calibration."""

    HUMIDITY = "humidity"
    ILLUMINANCE = "illuminance"
    TEMPERATURE = "temperature"


class Ep1OccupancyApplyButton(StrEnum):
    """ESPHome button roles pressed after mmWave occupancy number writes."""

    SET_DISTANCE = "set_distance"
    SET_SENSITIVITY = "set_sensitivity"


class Ep1OccupancyTuningKind(StrEnum):
    """ESPHome mmWave number knobs exposed under Settings → EP1 occupancy tuning."""

    MAX_DISTANCE = "max_distance"
    MIN_DISTANCE = "min_distance"
    OFF_LATENCY = "off_latency"
    ON_LATENCY = "on_latency"
    SUSTAIN_SENSITIVITY = "sustain_sensitivity"
    TRIGGER_DISTANCE = "trigger_distance"
    TRIGGER_SENSITIVITY = "trigger_sensitivity"


class Ep1ReadingComparison(StrEnum):
    """How an EP1 numeric reading is compared to a rule threshold."""

    ABOVE = "above"
    BELOW = "below"


class Ep1ReadingMetric(StrEnum):
    """EP1 climate / light fields usable in ``ep1_reading_compare`` conditions."""

    HUMIDITY_PCT = "humidity_pct"
    ILLUMINANCE_LX = "illuminance_lx"
    TEMPERATURE_C = "temperature_c"

    def display_label(self) -> str:
        """Short human label for Status details and summaries."""
        match self:
            case Ep1ReadingMetric.HUMIDITY_PCT:
                return "humidity"
            case Ep1ReadingMetric.ILLUMINANCE_LX:
                return "illuminance"
            case Ep1ReadingMetric.TEMPERATURE_C:
                return "temperature"

    def unit_label(self) -> str:
        """Unit suffix for Status details (fixed wire units)."""
        match self:
            case Ep1ReadingMetric.HUMIDITY_PCT:
                return "%"
            case Ep1ReadingMetric.ILLUMINANCE_LX:
                return "lx"
            case Ep1ReadingMetric.TEMPERATURE_C:
                return "°C"


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


class SensorChartWindow(StrEnum):
    """Time window for Automations → Data sensor charts."""

    LAST_5_MINUTES = "last_5_minutes"
    LAST_DAY = "last_day"
    LAST_HOUR = "last_hour"
    LAST_MINUTE = "last_minute"
    LAST_WEEK = "last_week"

    def duration_s(self) -> float:
        """Return how far back samples are included for this window."""
        match self:
            case SensorChartWindow.LAST_MINUTE:
                return 60.0
            case SensorChartWindow.LAST_5_MINUTES:
                return 300.0
            case SensorChartWindow.LAST_HOUR:
                return 3600.0
            case SensorChartWindow.LAST_DAY:
                return 86_400.0
            case SensorChartWindow.LAST_WEEK:
                return 604_800.0


class SensorCollectionKey(StrEnum):
    """Collectible sensor reading keys (EP1 v1; MAC device_id + this key)."""

    HUMIDITY_PCT = "humidity_pct"
    ILLUMINANCE_LX = "illuminance_lx"
    OCCUPANCY = "occupancy"
    TEMPERATURE_C = "temperature_c"

    def display_label(self) -> str:
        """Short human label for Automations → Data rows."""
        match self:
            case SensorCollectionKey.HUMIDITY_PCT:
                return "humidity"
            case SensorCollectionKey.ILLUMINANCE_LX:
                return "illuminance"
            case SensorCollectionKey.OCCUPANCY:
                return "occupancy"
            case SensorCollectionKey.TEMPERATURE_C:
                return "temperature"

    def unit_label(self) -> str | None:
        """Unit suffix when known; occupancy is unitless binary."""
        match self:
            case SensorCollectionKey.HUMIDITY_PCT:
                return "%"
            case SensorCollectionKey.ILLUMINANCE_LX:
                return "lx"
            case SensorCollectionKey.OCCUPANCY:
                return None
            case SensorCollectionKey.TEMPERATURE_C:
                return "°C"


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
