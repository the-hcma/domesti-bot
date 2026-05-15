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

export interface UIDeviceOut {
  id: string;
  family_id: string;
  label: string;
  kind: UIDeviceKind;
  state: UIDeviceState;
  exclude_from_global: boolean;
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
}

export interface TailwindTokenSetOut {
  configured: boolean;
  source: TailwindTokenSource;
  restart_required: boolean;
}
