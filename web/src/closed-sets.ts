/**
 * Const-object enums for fixed string sets (wire / UI closed vocabularies).
 *
 * Prefer these over bare string literals — see
 * ``.cursor/rules/typescript-closed-set-enums.mdc``.
 */

/** Evening/morning astronomical window endpoint (rules). */
export const AstronomicalWindowBoundary = {
  Midnight: "midnight",
} as const;
export type AstronomicalWindowBoundary =
  (typeof AstronomicalWindowBoundary)[keyof typeof AstronomicalWindowBoundary];

/** Global / per-family bulk-off scope. */
export const BulkOffScope = {
  Global: "global",
  Kasa: "kasa",
  Sonos: "sonos",
  Tailwind: "tailwind",
  Vizio: "vizio",
} as const;
export type BulkOffScope = (typeof BulkOffScope)[keyof typeof BulkOffScope];

/** Compact-tile SVG asset keys (and fallbacks). */
export const CompactIconKey = {
  Bulb: "bulb",
  Garage: "garage",
  GarageClosed: "garage_closed",
  GarageOpen: "garage_open",
  Occupancy: "occupancy",
  Outlet: "outlet",
  Speaker: "speaker",
  SpeakerPaused: "speaker_paused",
  SpeakerPlaying: "speaker_playing",
  SpeakerUnknown: "speaker_unknown",
  Tv: "tv",
  TvOff: "tv_off",
  TvOn: "tv_on",
} as const;
export type CompactIconKey = (typeof CompactIconKey)[keyof typeof CompactIconKey];

/** Confirm-dialog primary button tone. */
export const ConfirmButtonVariant = {
  Danger: "danger",
  Default: "default",
} as const;
export type ConfirmButtonVariant =
  (typeof ConfirmButtonVariant)[keyof typeof ConfirmButtonVariant];

/** Compact / comfortable landing layout (``#app[data-layout]``). */
export const DataLayout = {
  Comfortable: "comfortable",
  Compact: "compact",
} as const;
export type DataLayout = (typeof DataLayout)[keyof typeof DataLayout];

/** Stable device-manager family slug (UI tiles and rule actions). */
export const DeviceFamilyId = {
  AndroidTv: "androidtv",
  Ep1: "ep1",
  Kasa: "kasa",
  Sonos: "sonos",
  Tailwind: "tailwind",
  Vizio: "vizio",
} as const;
export type DeviceFamilyId = (typeof DeviceFamilyId)[keyof typeof DeviceFamilyId];

/** ``GET /health`` discovery field. */
export const DiscoveryStatus = {
  Failed: "failed",
  InProgress: "in_progress",
  Ready: "ready",
} as const;
export type DiscoveryStatus =
  (typeof DiscoveryStatus)[keyof typeof DiscoveryStatus];

/** EP1 reading compare direction. */
export const Ep1ReadingComparison = {
  Above: "above",
  Below: "below",
} as const;
export type Ep1ReadingComparison =
  (typeof Ep1ReadingComparison)[keyof typeof Ep1ReadingComparison];

/** EP1 reading compare metric. */
export const Ep1ReadingMetric = {
  HumidityPct: "humidity_pct",
  IlluminanceLx: "illuminance_lx",
  TemperatureC: "temperature_c",
} as const;
export type Ep1ReadingMetric =
  (typeof Ep1ReadingMetric)[keyof typeof Ep1ReadingMetric];

/** Geofence map draw interaction state. */
export const GeofenceDrawState = {
  Idle: "idle",
  PlacingCenter: "placing-center",
  PlacingRadius: "placing-radius",
} as const;
export type GeofenceDrawState =
  (typeof GeofenceDrawState)[keyof typeof GeofenceDrawState];

/** Kasa credentials resolution (no CLI path). */
export const KasaCredentialsSource = {
  Database: "database",
  Env: "env",
  None: "none",
} as const;
export type KasaCredentialsSource =
  (typeof KasaCredentialsSource)[keyof typeof KasaCredentialsSource];

/** EP1 / Tailwind / Vizio token or PSK resolution (includes CLI). */
export const ManagedSecretSource = {
  Cli: "cli",
  Database: "database",
  Env: "env",
  None: "none",
} as const;
export type ManagedSecretSource =
  (typeof ManagedSecretSource)[keyof typeof ManagedSecretSource];

export type Ep1NoisePreSharedKeySource = ManagedSecretSource;
export type TailwindTokenSource = ManagedSecretSource;
export type VizioAuthSource = ManagedSecretSource;

/** My Tracks sync dialog target. */
export const MyTracksSyncKind = {
  Geofences: "geofences",
  Users: "users",
} as const;
export type MyTracksSyncKind =
  (typeof MyTracksSyncKind)[keyof typeof MyTracksSyncKind];

/** Per-device command when an automation rule fires. */
export const RuleActionType = {
  Close: "close",
  Open: "open",
  Pause: "pause",
  Resume: "resume",
  TurnOff: "turn_off",
  TurnOn: "turn_on",
} as const;
export type RuleActionType = (typeof RuleActionType)[keyof typeof RuleActionType];

/** Rule condition discriminant (``RuleConditionOut.type``). */
export const RuleConditionType = {
  AfterLocalTime: "after_local_time",
  AfterSunset: "after_sunset",
  All: "all",
  Any: "any",
  BeforeLocalTime: "before_local_time",
  BeforeSunrise: "before_sunrise",
  Daylight: "daylight",
  DaysOfWeek: "days_of_week",
  DevicesAllInState: "devices_all_in_state",
  DevicesAnyInState: "devices_any_in_state",
  DevicesAnyInStateForS: "devices_any_in_state_for_s",
  Ep1ReadingCompare: "ep1_reading_compare",
  LocalTimeWindow: "local_time_window",
  UsersInsideGeofence: "users_inside_geofence",
  UsersInsideGeofenceForS: "users_inside_geofence_for_s",
  UsersMinDistanceFromHomeM: "users_min_distance_from_home_m",
  UsersOutsideGeofence: "users_outside_geofence",
  UsersOutsideGeofenceForS: "users_outside_geofence_for_s",
} as const;
export type RuleConditionType =
  (typeof RuleConditionType)[keyof typeof RuleConditionType];

/** Rule reference / validation issue kinds. */
export const RuleReferenceIssueKind = {
  DiscoveryPending: "discovery_pending",
  GeofenceEdgeGraceDisabled: "geofence_edge_grace_disabled",
  MissingNotificationEmail: "missing_notification_email",
  MissingSmtp: "missing_smtp",
  NonCanonicalDeviceId: "non_canonical_device_id",
  StaleDeviceDisplayName: "stale_device_display_name",
  UnknownDevice: "unknown_device",
  UnknownGeofence: "unknown_geofence",
  UnknownUser: "unknown_user",
  UnsupportedDeviceAction: "unsupported_device_action",
} as const;
export type RuleReferenceIssueKind =
  (typeof RuleReferenceIssueKind)[keyof typeof RuleReferenceIssueKind];

/** How a rule can be armed (``triggers`` entries). */
export const RuleTrigger = {
  DeviceState: "device_state",
  DwellSatisfied: "dwell_satisfied",
  EdgeTrue: "edge_true",
  Scheduled: "scheduled",
} as const;
export type RuleTrigger = (typeof RuleTrigger)[keyof typeof RuleTrigger];

/** Automations hub top-level tabs. */
export const RulesTabId = {
  Conditions: "conditions",
  Geofences: "geofences",
  Mail: "mail",
  Rules: "rules",
  Status: "status",
  Users: "users",
  Vacation: "vacation",
} as const;
export type RulesTabId = (typeof RulesTabId)[keyof typeof RulesTabId];

/** Fernet secrets-key resolution. */
export const SecretsKeySource = {
  Env: "env",
  File: "file",
  None: "none",
} as const;
export type SecretsKeySource =
  (typeof SecretsKeySource)[keyof typeof SecretsKeySource];

/** Where a Settings Test probe got credentials. */
export const SettingsCredentialsTestSource = {
  Cli: "cli",
  Database: "database",
  Env: "env",
  Form: "form",
} as const;
export type SettingsCredentialsTestSource =
  (typeof SettingsCredentialsTestSource)[keyof typeof SettingsCredentialsTestSource];

/** Settings hub tabs. */
export const SettingsTabId = {
  Ep1: "ep1",
  Kasa: "kasa",
  MyTracks: "my-tracks",
  Tailwind: "tailwind",
  Vizio: "vizio",
} as const;
export type SettingsTabId = (typeof SettingsTabId)[keyof typeof SettingsTabId];

/** Color-scheme preference (localStorage / ``prefers-color-scheme``). */
export const ThemePreference = {
  Dark: "dark",
  Light: "light",
} as const;
export type ThemePreference =
  (typeof ThemePreference)[keyof typeof ThemePreference];

/** Compact tile tone (CSS ``data-tone``). */
export const TileTone = {
  Active: "active",
  Inactive: "inactive",
  Unknown: "unknown",
} as const;
export type TileTone = (typeof TileTone)[keyof typeof TileTone];

/** Action / status toast variant. */
export const ToastVariant = {
  Error: "error",
  Info: "info",
  Success: "success",
} as const;
export type ToastVariant = (typeof ToastVariant)[keyof typeof ToastVariant];

/** Presence / location feed provenance on user location payloads. */
export const UserLocationSource = {
  MyTracks: "my-tracks",
} as const;
export type UserLocationSource =
  (typeof UserLocationSource)[keyof typeof UserLocationSource];

/** Vacation-mode Settings test email kind. */
export const VacationModeTestEmailKind = {
  Anomaly: "anomaly",
  Arm: "arm",
  Disarm: "disarm",
} as const;
export type VacationModeTestEmailKind =
  (typeof VacationModeTestEmailKind)[keyof typeof VacationModeTestEmailKind];
