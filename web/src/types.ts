// Mirror of the Pydantic schemas in `app/api/schemas.py`. Keep these field
// names in sync with the server — `test_ui_state_out_is_a_pydantic_model_…`
// guards the Python side; the TypeScript side is guarded by `tsc --strict`
// at `pnpm run typecheck`.

export interface MetaOut {
  version: string;
  commit: string;
}

export type UIDeviceKind = "switch" | "speaker" | "door";

export type UIDeviceState =
  | "on"
  | "off"
  | "playing"
  | "paused"
  | "open"
  | "closed"
  | "unknown";

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
  exclude_from_global: boolean;
  /** Configured radio streams for Sonos zones (empty for other families). */
  stream_favorites: UISonosStreamFavoriteOut[];
}

export interface UIFamilyOut {
  id: string;
  label: string;
  color: string;
  devices: UIDeviceOut[];
}

export interface UIStateOut {
  families: UIFamilyOut[];
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
}

export type SecretsKeySource = "env" | "file" | "none";

export type TailwindTokenSource = "cli" | "env" | "database" | "none";

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

// --- Rule engine (mirror planned ``app/api/schemas.py`` rule models) ---

export type RuleTrigger = "edge_true" | "while_true";

export type RuleActionType =
  | "turn_on"
  | "turn_off"
  | "open"
  | "close"
  | "pause"
  | "resume";

export interface RuleDeviceActionOut {
  action: RuleActionType;
  device_id: string;
  family_id: string;
}

export type RuleConditionOut =
  | {
      type: "participants_inside_geofence";
      geofence_id: string;
      participant_ids: string[];
    }
  | {
      type: "participants_outside_geofence";
      geofence_id: string;
      participant_ids: string[];
    }
  | { type: "after_sunset"; offset_minutes: number }
  | { type: "before_sunrise"; offset_minutes: number }
  | { type: "after_local_time"; time_hhmm: string }
  | { type: "before_local_time"; time_hhmm: string }
  | {
      /** JavaScript ``Date.getDay()`` values: 0 = Sunday … 6 = Saturday. */
      type: "days_of_week";
      days: number[];
    };

export interface TimeConditionTemplateOut {
  template_id: string;
  label: string;
  type: "after_local_time" | "before_local_time";
  time_hhmm: string;
}

export interface RuleConditionsOut {
  all: RuleConditionOut[];
}

export interface RuleOut {
  id: string;
  label: string;
  enabled: boolean;
  trigger: RuleTrigger;
  cooldown_s: number;
  /** Fixes with horizontal accuracy worse than this (meters) are ignored. */
  min_fix_accuracy_m: number;
  /** Send email when this automation fires (requires SMTP in Mail tab). */
  notify_on_fire: boolean;
  notification_email: string | null;
  conditions: RuleConditionsOut;
  device_actions: RuleDeviceActionOut[];
}

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

export interface MyTracksParticipantsSyncOut {
  source: "my-tracks";
  last_synced_at: string | null;
  participant_count: number;
  webhook_ready: boolean;
}

export interface SmtpTestEmailIn extends SmtpConfigIn {
  to_address: string;
}

export interface SmtpTestEmailOut {
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

export interface ParticipantOut {
  participant_id: string;
  display_name: string;
  /** Phone or tracker that reports this participant's location (my-tracks device). */
  tracking_device_label: string;
  enabled: boolean;
}

export interface ParticipantFixOut {
  lat: number;
  lon: number;
  accuracy_m: number | null;
  received_at: string;
  source: string | null;
}

export interface ParticipantStatusOut extends ParticipantOut {
  last_fix: ParticipantFixOut | null;
  inside_geofence_ids: string[];
  age_seconds: number | null;
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

export interface RuleStatusSummaryOut {
  id: string;
  label: string;
  enabled: boolean;
  condition_currently_true: boolean;
  conditions: RuleConditionStatusOut[];
  last_fired_at: string | null;
  last_error: string | null;
}

export interface RulesEvaluatorOut {
  last_run_at: string | null;
  next_sun_check_at: string | null;
}

export interface RulesStatusOut {
  participants: ParticipantStatusOut[];
  geofences: GeofenceOut[];
  rules: RuleStatusSummaryOut[];
  sun: RulesSunOut;
  evaluator: RulesEvaluatorOut;
  using_mock: boolean;
}

export interface SettingsLocationOut {
  lat: number;
  lon: number;
  timezone: string;
  home_label: string | null;
}

export interface RuleActionDeviceOut {
  family_id: string;
  device_id: string;
  label: string;
  kind: UIDeviceKind;
}
