// Mirror of the Pydantic schemas in `app/api/schemas.py`. Keep these field
// names in sync with the server — `test_ui_state_out_is_a_pydantic_model_…`
// guards the Python side; the TypeScript side is guarded by `tsc --strict`
// at `pnpm run typecheck`.

export interface MetaOut {
  version: string;
  commit: string;
}

export interface HealthOut {
  status: string;
  service: string;
  ready: boolean;
  discovery: "ready" | "in_progress" | "failed";
  error: string | null;
}

/**
 * Device tile kind. ``occupancy`` is room occupancy (EP1 mmWave/PIR), not
 * My Tracks presence / user / location.
 */
export type UIDeviceKind = "door" | "occupancy" | "speaker" | "switch";

/** Mirror of Python ``DeviceConditionState`` (rules / actions; no ``unknown``). */
export type DeviceConditionState =
  | "clear"
  | "closed"
  | "occupied"
  | "off"
  | "on"
  | "open"
  | "paused"
  | "playing";

/**
 * Tile state from ``GET /v1/ui/state``.
 * UI-only superset of ``DeviceConditionState`` — ``unknown`` covers transient
 * readings (Tailwind OPENING/CLOSING, Sonos pre-poll, Vizio auth gaps).
 */
export type UIDeviceState = DeviceConditionState | "unknown";

/**
 * Environmental readings for ``kind=occupancy`` (EP1). Units are fixed:
 * humidity ``%``, illuminance ``lx``, temperature ``°C`` and ``°F``
 * (both present when either is known — server derives the missing unit).
 */
export interface UIOccupancyReadingsOut {
  humidity_pct: number | null;
  illuminance_lx: number | null;
  temperature_c: number | null;
  temperature_f: number | null;
}

export interface UISonosStreamFavoriteOut {
  name: string;
  uri: string;
}

export interface UIDeviceOut {
  id: string;
  family_id: string;
  label: string;
  kind: UIDeviceKind;
  state: UIDeviceState;
  /** Server-resolved compact-tile SVG key (bulb, outlet, garage, speaker, …). */
  compact_icon: string;
  /** Normalized MAC address (required; same hardware identity as ``id`` when single-endpoint). */
  mac_address: string;
  /** Current IP / hostname when known. */
  host?: string | null;
  /** Family-specific identity lines (RINCON, door index, model, …). */
  identity_details?: string[];
  exclude_from_global: boolean;
  hide_on_mobile: boolean;
  /** Temperature / humidity / illuminance for occupancy tiles; null otherwise. */
  occupancy_readings?: UIOccupancyReadingsOut | null;
  /** Configured radio streams for Sonos zones (empty for other families). */
  stream_favorites: UISonosStreamFavoriteOut[];
}

export interface UIFamilyOut {
  id: string;
  label: string;
  color: string;
  devices: UIDeviceOut[];
}

export interface UIOperatorAlertOut {
  message: string;
  reason_code: string;
  recorded_at: number;
}

export interface UIStateOut {
  families: UIFamilyOut[];
  operator_alert?: UIOperatorAlertOut | null;
}

export interface UIBulkActionOut {
  affected: string[];
  skipped: string[];
}

export interface UIGlobalBulkActionItem {
  family_id: string;
  device_id: string;
}

export interface UIGlobalBulkActionOut {
  affected: UIGlobalBulkActionItem[];
  skipped: UIGlobalBulkActionItem[];
}

export interface UIDeviceActionOut {
  device: UIDeviceOut;
}

export interface UIPreferenceOut {
  family_id: string;
  device_id: string;
  exclude_from_global: boolean;
  hide_on_mobile: boolean;
}

export type KasaCredentialsSource = "env" | "database" | "none";

export type SecretsKeySource = "env" | "file" | "none";

export type Ep1NoisePreSharedKeySource = "cli" | "env" | "database" | "none";

export type TailwindTokenSource = "cli" | "env" | "database" | "none";

export interface KasaCredentialsSetOut {
  configured: boolean;
  source: KasaCredentialsSource;
  restart_required: boolean;
}

export interface KasaCredentialsSettingsOut {
  configured: boolean;
  source: KasaCredentialsSource;
  secrets_key_configured: boolean;
  secrets_key_source: SecretsKeySource;
  stored_in_database: boolean;
  stored_password: string | null;
  stored_username: string | null;
  password_stored: boolean;
  hosts_requiring_klap_auth: string[];
  skipped_auth_hosts: string[];
}

export interface KasaCredentialsTestIn {
  password?: string | null;
  username?: string | null;
}

export type SettingsCredentialsTestSource =
  | "cli"
  | "database"
  | "env"
  | "form";

export interface SettingsCredentialsTestOut {
  detail: string;
  ok: boolean;
  source?: SettingsCredentialsTestSource | null;
}

export interface Ep1NoisePreSharedKeySetOut {
  configured: boolean;
  source: Ep1NoisePreSharedKeySource;
  restart_required: boolean;
}

export interface Ep1NoisePreSharedKeySettingsOut {
  configured: boolean;
  source: Ep1NoisePreSharedKeySource;
  secrets_key_configured: boolean;
  secrets_key_source: SecretsKeySource;
  stored_in_database: boolean;
  stored_noise_psk: string | null;
}

export interface Ep1NoisePreSharedKeyTestIn {
  host?: string | null;
  noise_psk?: string | null;
}

export interface TailwindTokenSettingsOut {
  configured: boolean;
  source: TailwindTokenSource;
  secrets_key_configured: boolean;
  secrets_key_source: SecretsKeySource;
  stored_in_database: boolean;
  stored_token: string | null;
}

export interface TailwindTokenSetOut {
  configured: boolean;
  source: TailwindTokenSource;
  restart_required: boolean;
}

export interface TailwindTokenTestIn {
  host?: string | null;
  token?: string | null;
}

export type VizioAuthSource = "cli" | "env" | "database" | "none";

export interface VizioAuthTestIn {
  token?: string | null;
}

export interface VizioTvSettingsOut {
  device_id: string;
  mac: string | null;
  host: string;
  port: number;
  display_name: string | null;
  auth_configured: boolean;
  auth_source: VizioAuthSource;
  stored_token: string | null;
}

export interface VizioTvsSettingsOut {
  secrets_key_configured: boolean;
  secrets_key_source: SecretsKeySource;
  tvs: VizioTvSettingsOut[];
}

export interface VizioAuthTokenSetOut {
  configured: boolean;
  device_id: string;
  restart_required: boolean;
}

export interface VizioPairBeginOut {
  device_id: string;
  challenge_type: number;
  pairing_req_token: number;
}

export interface VizioPairCompleteOut {
  configured: boolean;
  device_id: string;
  restart_required: boolean;
}

// --- Rule engine (mirror planned ``app/api/schemas.py`` rule models) ---

export type RuleTrigger = "device_state" | "dwell_satisfied" | "edge_true" | "scheduled";

export type RuleActionType =
  | "turn_on"
  | "turn_off"
  | "open"
  | "close"
  | "pause"
  | "resume";

export interface RuleDeviceActionOut {
  action: RuleActionType;
  delay_s?: number | null;
  device_id: string;
  /** Non-authoritative friendly name snapshot; device_id (MAC) is authoritative. */
  display_name?: string | null;
  family_id: string;
}

export type RuleConditionOut =
  | {
      type: "users_inside_geofence";
      geofence_id: string;
      user_ids: string[];
    }
  | {
      type: "users_inside_geofence_for_s";
      geofence_id: string;
      user_ids: string[];
      min_inside_s: number;
    }
  | {
      type: "users_min_distance_from_home_m";
      user_ids: string[];
      min_distance_m: number;
    }
  | {
      type: "users_outside_geofence";
      geofence_id: string;
      user_ids: string[];
    }
  | {
      type: "users_outside_geofence_for_s";
      geofence_id: string;
      user_ids: string[];
      min_outside_s: number;
    }
  | {
      type: "after_sunset";
      offset_minutes: number;
      /** Default ``midnight`` — evening window ends at local midnight. */
      window_end?: "midnight";
    }
  | {
      type: "all";
      conditions: RuleConditionOut[];
    }
  | {
      type: "any";
      conditions: RuleConditionOut[];
    }
  | {
      type: "before_sunrise";
      offset_minutes: number;
      /** Default ``midnight`` — morning window starts at local midnight. */
      window_start?: "midnight";
    }
  | { type: "daylight" }
  | { type: "after_local_time"; time_hhmm: string }
  | { type: "before_local_time"; time_hhmm: string }
  | { type: "local_time_window"; start_hhmm: string; end_hhmm: string }
  | {
      /** JavaScript ``Date.getDay()`` values: 0 = Sunday … 6 = Saturday. */
      type: "days_of_week";
      days: number[];
    }
  | {
      type: "devices_all_in_state";
      devices: RuleConditionDeviceRefOut[];
      state: DeviceConditionState;
    }
  | {
      type: "devices_any_in_state";
      devices: RuleConditionDeviceRefOut[];
      state: DeviceConditionState;
    }
  | {
      type: "devices_any_in_state_for_s";
      devices: RuleConditionDeviceRefOut[];
      min_duration_s: number;
      state: DeviceConditionState;
    };

export interface RuleConditionDeviceRefOut {
  device_id: string;
  /** Non-authoritative friendly name snapshot; device_id (MAC) is authoritative. */
  display_name?: string | null;
  family_id: string;
}

export interface TimeConditionTemplateOut {
  template_id: string;
  label: string;
  start_hhmm: string;
  end_hhmm: string;
}

export interface RuleConditionsOut {
  all: RuleConditionOut[];
}

interface RuleOutShared {
  id: string;
  label: string;
  enabled: boolean;
  cooldown_s: number;
  /** Retry geofence edges for this many seconds after an accuracy skip (0 disables; default 120). */
  accuracy_edge_grace_s?: number;
  /** Locations with horizontal accuracy worse than this (meters) are ignored. */
  min_location_accuracy_m: number;
  /** Send email when this automation fires (requires SMTP in Mail tab). */
  notify_on_fire: boolean;
  notification_emails: string[];
  conditions: RuleConditionsOut;
  device_actions: RuleDeviceActionOut[];
  /** 5-field cron (minute hour day month weekday); home timezone from settings. */
  schedule_cron: string | null;
  triggers: RuleTrigger[];
  /** At most one fire per local calendar day (home timezone). */
  fire_once_per_local_day?: boolean;
}

export type RuleOut = RuleOutShared;

export interface SmtpConfigIn {
  host: string;
  port: number;
  username: string;
  /** Null keeps the stored password on update. */
  password: string | null;
  /** Mail domain for default From address (e.g. ``hcma.info``). */
  mail_domain: string;
  from_address: string;
}

export interface SmtpConfigOut {
  host: string;
  port: number;
  username: string;
  mail_domain: string;
  from_address: string;
  password_configured: boolean;
  last_test_recipient: string | null;
}

export interface LocationHistoryRetentionIn {
  max_age_hours: number;
  min_keep_count: number;
  unlimited: boolean;
}

export interface LocationHistoryRetentionOut {
  max_age_hours: number;
  min_keep_count: number;
  unlimited: boolean;
}

export interface MyTracksGeofencesSyncOut {
  geofence_count: number;
  last_synced_at: string | null;
  source: "my-tracks";
}

export interface MyTracksUsersSyncOut {
  last_synced_at: string | null;
  user_count: number;
  source: "my-tracks";
  webhook_ready: boolean;
}

export interface MyTracksPairIn {
  domain: string;
  location_history_retention: LocationHistoryRetentionIn;
  password: string;
  username: string;
}

export interface MyTracksRelayKeySettingsOut {
  configured: boolean;
  stored_relay_key: string | null;
}

export interface LocationRequestRateLimitsOut {
  device_cooldown_seconds: number;
  user_cooldown_seconds: number;
  user_cooldown_seconds_by_reason?: Record<string, number> | null;
}

export interface MyTracksLocationMonitoringIn {
  approach_distance_m: number;
}

export interface MyTracksLocationMonitoringOut {
  approach_distance_m: number;
  approach_request_interval_s: number;
}

export interface MyTracksPairStatusOut {
  domain: string;
  domesti_public_base_url: string | null;
  last_pair_error: string | null;
  last_verify_at: string | null;
  last_verify_ok: boolean | null;
  location_history_retention: LocationHistoryRetentionOut;
  location_updates_accepted: boolean;
  mytracks_location_updates_enabled: boolean | null;
  mytracks_location_request_rate_limits: LocationRequestRateLimitsOut | null;
  mytracks_remote_request_location_enabled: boolean | null;
  paired_at: string | null;
  user_location_test_url: string | null;
  user_location_update_url: string | null;
  relay_key_configured: boolean;
  username: string;
}

export interface MyTracksSettingsIn {
  domain: string;
  username: string;
}

export interface MyTracksSettingsOut {
  domain: string;
  username: string;
}

export interface MyTracksSyncIn {
  password: string;
  username?: string;
}

export interface MyTracksCredentialsTestIn {
  domain?: string | null;
  password: string;
  username?: string | null;
}

export interface SmtpTestEmailIn extends SmtpConfigIn {
  to_address: string;
}

export interface SmtpTestEmailOut {
  message: string;
  ok: boolean;
}

export interface VacationModeSettingsOut {
  enabled: boolean;
  hysteresis_s: number;
  min_distance_m: number;
  min_location_accuracy_m: number;
  notification_emails: string[];
  notify_on_transition: boolean;
  user_ids: string[];
}

export interface VacationModeSettingsStatusOut extends VacationModeSettingsOut {
  armed: boolean;
}

export interface VacationModeTestEmailIn {
  kind: "anomaly" | "arm" | "disarm";
}

export interface VacationModeTestEmailOut {
  message: string;
  ok: boolean;
}

export interface GeofenceOut {
  geofence_id: string;
  label: string;
  center_lat: number;
  center_lon: number;
  radius_m: number;
  enabled: boolean;
  owntracks_rid?: string | null;
}

export interface UserOut {
  display_name: string;
  enabled: boolean;
  first_name: string;
  home_wifi_bssid: string | null;
  home_wifi_ssid: string | null;
  last_name: string;
  tracking_device_label: string;
  user_id: string;
}

export interface ObservedWifiNetworkOut {
  last_seen_at: string;
  wifi_bssid: string;
  wifi_ssid: string;
}

export interface UserHomeWifiIn {
  wifi_bssid: string | null;
  wifi_ssid: string | null;
}

export interface UserLocationOut {
  lat: number;
  lon: number;
  accuracy_m: number | null;
  battery_level?: number | null;
  connection_type?: string | null;
  fix_at: string;
  fix_source?: string | null;
  reported_at: string;
  source: string | null;
  trigger?: string | null;
  wifi_bssid?: string | null;
  wifi_ssid?: string | null;
}

export interface UserStatusOut extends UserOut {
  age_seconds: number | null;
  inside_geofence_ids: string[];
  last_location: UserLocationOut | null;
}

export interface RulesSunOut {
  sunset_at: string;
  sunrise_at: string;
  is_dark: boolean;
}

export interface RuleConditionStatusOut {
  condition: RuleConditionOut;
  detail: string;
  label: string;
  met: boolean;
}

export interface RuleReferenceIssueOut {
  kind:
    | "discovery_pending"
    | "geofence_edge_grace_disabled"
    | "missing_notification_email"
    | "missing_smtp"
    | "non_canonical_device_id"
    | "stale_device_display_name"
    | "unknown_device"
    | "unknown_geofence"
    | "unknown_user";
  reference: string;
  detail: string;
}

export interface RuleStatusSummaryOut {
  id: string;
  label: string;
  enabled: boolean;
  condition_currently_true: boolean;
  conditions: RuleConditionStatusOut[];
  last_fired_at: string | null;
  last_error: string | null;
  next_evaluate_at: string | null;
  scheduled_detail: string | null;
  reference_issues: RuleReferenceIssueOut[];
  triggers: RuleTrigger[];
}

export interface RulesEvaluatorOut {
  last_run_at: string | null;
  next_sun_check_at: string | null;
}

export interface RulesStatusOut {
  users: UserStatusOut[];
  geofences: GeofenceOut[];
  rules: RuleStatusSummaryOut[];
  sun: RulesSunOut;
  evaluator: RulesEvaluatorOut;
}

export interface SettingsLocationOut {
  home_configured: boolean;
  home_label: string | null;
  lat: number;
  lon: number;
  timezone: string;
  wifi_home_geofence_id?: string | null;
  wifi_home_presence_enabled?: boolean;
}

export interface RuleActionDeviceOut {
  family_id: string;
  device_id: string;
  label: string;
  kind: UIDeviceKind;
}
