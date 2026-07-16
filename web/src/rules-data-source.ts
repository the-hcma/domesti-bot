// Rules hub data access — mock store for rules; SMTP uses the HTTP API when available.

import { api, authHeaders } from "./api.js";
import {
  createMockStoreSeed,
  haversineM,
  mockSunRow,
  type MockMyTracksSettings,
  type MockStoreSeed,
} from "./rules-mock-fixtures.js";
import { referencesGeofenceId } from "./rule-summary.js";
import type {
  GeofenceOut,
  MyTracksGeofencesSyncOut,
  MyTracksUsersSyncOut,
  MyTracksSettingsIn,
  MyTracksSettingsOut,
  MyTracksSyncIn,
  ObservedWifiNetworkOut,
  UserHomeWifiIn,
  UserLocationOut,
  UserOut,
  UserStatusOut,
  RuleActionDeviceOut,
  RuleOut,
  RulesStatusOut,
  SettingsLocationOut,
  SmtpConfigIn,
  SmtpConfigOut,
  SmtpTestEmailIn,
  SmtpTestEmailOut,
  TimeConditionTemplateOut,
  VacationModeSettingsOut,
  VacationModeSettingsStatusOut,
  VacationModeTestEmailIn,
  VacationModeTestEmailOut,
} from "./types.js";

const FILE_BACKED_RULES_ERROR =
  "Rules are read from automation-rules.json on the server; edit that file and restart to change them.";

export interface RulesDataSource {
  isRulesFileBacked(): boolean;
  getStatus(): Promise<RulesStatusOut>;
  listGeofences(): Promise<GeofenceOut[]>;
  saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut>;
  deleteGeofence(geofenceId: string): Promise<void>;
  deleteUser(userId: string): Promise<void>;
  getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut>;
  getMyTracksUsersSync(): Promise<MyTracksUsersSyncOut>;
  getMyTracksSettings(): Promise<MyTracksSettingsOut | null>;
  listUserStatus(): Promise<UserStatusOut[]>;
  listUsers(): Promise<UserOut[]>;
  listUserObservedWifi(userId: string): Promise<ObservedWifiNetworkOut[]>;
  resetMyTracksSettings(): Promise<void>;
  saveMyTracksSettings(config: MyTracksSettingsIn): Promise<MyTracksSettingsOut>;
  saveUser(user: UserOut): Promise<UserOut>;
  setUserHomeWifi(
    userId: string,
    homeWifi: UserHomeWifiIn,
  ): Promise<UserOut>;
  syncGeofencesFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut>;
  syncUsersFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksUsersSyncOut>;
  listRules(): Promise<RuleOut[]>;
  listTimeConditionTemplates(): Promise<TimeConditionTemplateOut[]>;
  getRule(ruleId: string): Promise<RuleOut | null>;
  saveRule(rule: RuleOut): Promise<RuleOut>;
  deleteRule(ruleId: string): Promise<void>;
  deleteTimeConditionTemplate(templateId: string): Promise<void>;
  setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut>;
  getSettingsLocation(): Promise<SettingsLocationOut>;
  getSmtpConfig(): Promise<SmtpConfigOut | null>;
  getVacationModeSettings(): Promise<VacationModeSettingsStatusOut>;
  saveSettingsLocation(location: SettingsLocationOut): Promise<SettingsLocationOut>;
  resetSmtpConfig(): Promise<void>;
  saveSmtpConfig(config: SmtpConfigIn): Promise<SmtpConfigOut>;
  saveVacationModeSettings(
    settings: VacationModeSettingsOut,
  ): Promise<VacationModeSettingsStatusOut>;
  sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut>;
  sendVacationModeTestEmail(
    input: VacationModeTestEmailIn,
  ): Promise<VacationModeTestEmailOut>;
  saveTimeConditionTemplate(
    template: TimeConditionTemplateOut,
  ): Promise<TimeConditionTemplateOut>;
  listActionDevices(): Promise<RuleActionDeviceOut[]>;
}

function requireSyncPassword(credentials: MyTracksSyncIn): string {
  const password = credentials.password.trim();
  if (password === "") {
    throw new Error("Expected My Tracks admin password, got empty value");
  }
  return password;
}

function requireMyTracksDomain(settings: MockMyTracksSettings | null): string {
  const domain = settings?.domain.trim() ?? "";
  if (domain === "") {
    throw new Error(
      "Expected My Tracks domain in Settings, got empty value — configure domain first",
    );
  }
  return domain;
}

function cloneSeed(seed: MockStoreSeed): MockStoreSeed {
  return structuredClone(seed);
}

function userInsideGeofence(
  location: { lat: number; lon: number } | null,
  geofence: GeofenceOut,
): boolean {
  if (location === null || !geofence.enabled) {
    return false;
  }
  const dist = haversineM(
    location.lat,
    location.lon,
    geofence.center_lat,
    geofence.center_lon,
  );
  return dist <= geofence.radius_m;
}

export class MockRulesDataSource implements RulesDataSource {
  private store: MockStoreSeed;

  constructor(seed: MockStoreSeed = createMockStoreSeed()) {
    this.store = cloneSeed(seed);
  }

  async getStatus(): Promise<RulesStatusOut> {
    const users = await this.listUserStatus();
    return {
      users,
      geofences: structuredClone(this.store.geofences),
      rules: this.store.rules.map((rule) => ({
        id: rule.id,
        label: rule.label,
        enabled: rule.enabled,
        condition_currently_true: false,
        conditions: [],
        last_fired_at: this.store.rule_last_fired_at[rule.id] ?? null,
        last_error: null,
        next_evaluate_at: null,
        scheduled_detail: null,
        reference_issues: [],
        triggers: rule.triggers,
      })),
      sun: mockSunRow(),
      evaluator: {
        last_run_at: null,
        next_sun_check_at: null,
      },
    };
  }

  getStoreSeed(): MockStoreSeed {
    return cloneSeed(this.store);
  }

  isRulesFileBacked(): boolean {
    return false;
  }

  async listGeofences(): Promise<GeofenceOut[]> {
    return structuredClone(this.store.geofences);
  }

  async saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut> {
    const idx = this.store.geofences.findIndex(
      (g) => g.geofence_id === geofence.geofence_id,
    );
    if (idx >= 0) {
      this.store.geofences[idx] = structuredClone(geofence);
    } else {
      this.store.geofences.push(structuredClone(geofence));
    }
    return structuredClone(geofence);
  }

  async deleteGeofence(geofenceId: string): Promise<void> {
    const inUse = this.store.rules.some((rule) =>
      rule.conditions.all.some((condition) =>
        referencesGeofenceId(condition, geofenceId),
      ),
    );
    if (inUse) {
      throw new Error(
        `Expected geofence not referenced by rules, got ${geofenceId} in use`,
      );
    }
    this.store.geofences = this.store.geofences.filter(
      (g) => g.geofence_id !== geofenceId,
    );
  }

  async deleteUser(userId: string): Promise<void> {
    this.store.users = this.store.users.filter(
      (p) => p.user_id !== userId,
    );
    delete this.store.user_locations[userId];
  }

  async getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return {
      source: "my-tracks",
      last_synced_at: this.store.geofences_sync.last_synced_at,
      geofence_count: this.store.geofences.length,
    };
  }

  async getMyTracksUsersSync(): Promise<MyTracksUsersSyncOut> {
    return {
      source: "my-tracks",
      last_synced_at: this.store.users_sync.last_synced_at,
      user_count: this.store.users.length,
      webhook_ready: true,
    };
  }

  async listUserStatus(): Promise<UserStatusOut[]> {
    const now = Date.now();
    return this.store.users.map((user) => {
      const reading = this.store.user_locations[user.user_id] ?? null;
      const age_seconds =
        reading === null
          ? null
          : Math.max(0, Math.floor((now - Date.parse(reading.reported_at)) / 1000));
      const inside_geofence_ids = this.store.geofences
        .filter((geofence) => userInsideGeofence(reading, geofence))
        .map((geofence) => geofence.geofence_id);
      return {
        ...user,
        age_seconds,
        inside_geofence_ids,
        last_location: reading,
      };
    });
  }

  async listUsers(): Promise<UserOut[]> {
    return structuredClone(this.store.users);
  }

  async listUserObservedWifi(_userId: string): Promise<ObservedWifiNetworkOut[]> {
    return [];
  }

  async saveUser(user: UserOut): Promise<UserOut> {
    const idx = this.store.users.findIndex(
      (p) => p.user_id === user.user_id,
    );
    if (idx >= 0) {
      this.store.users[idx] = structuredClone(user);
    } else {
      this.store.users.push(structuredClone(user));
    }
    return structuredClone(user);
  }

  async setUserHomeWifi(
    userId: string,
    homeWifi: UserHomeWifiIn,
  ): Promise<UserOut> {
    const user = this.store.users.find((row) => row.user_id === userId);
    if (user === undefined) {
      throw new Error(`Expected user ${JSON.stringify(userId)}, got none`);
    }
    const updated: UserOut = {
      ...user,
      home_wifi_bssid: homeWifi.wifi_bssid,
      home_wifi_ssid: homeWifi.wifi_ssid,
    };
    return this.saveUser(updated);
  }

  async getMyTracksSettings(): Promise<MyTracksSettingsOut | null> {
    const cfg = this.store.my_tracks_settings;
    if (cfg === null) {
      return null;
    }
    return {
      domain: cfg.domain,
      username: cfg.username,
    };
  }

  async resetMyTracksSettings(): Promise<void> {
    this.store.my_tracks_settings = null;
  }

  async saveMyTracksSettings(
    config: MyTracksSettingsIn,
  ): Promise<MyTracksSettingsOut> {
    this.store.my_tracks_settings = {
      domain: config.domain.trim(),
      username: config.username.trim(),
    };
    const saved = await this.getMyTracksSettings();
    if (saved === null) {
      throw new Error("Expected My Tracks settings after save, got null");
    }
    return saved;
  }

  async syncGeofencesFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut> {
    requireMyTracksDomain(this.store.my_tracks_settings);
    requireSyncPassword(credentials);
    this.store.geofences = structuredClone(this.store.my_tracks_geofence_catalog);
    this.store.geofences_sync.last_synced_at = new Date().toISOString();
    return this.getMyTracksGeofencesSync();
  }

  async syncUsersFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksUsersSyncOut> {
    requireMyTracksDomain(this.store.my_tracks_settings);
    requireSyncPassword(credentials);
    this.store.users = structuredClone(this.store.my_tracks_user_catalog);
    this.store.users_sync.last_synced_at = new Date().toISOString();
    return this.getMyTracksUsersSync();
  }

  async listRules(): Promise<RuleOut[]> {
    return structuredClone(this.store.rules);
  }

  async listTimeConditionTemplates(): Promise<TimeConditionTemplateOut[]> {
    return structuredClone(this.store.time_condition_templates);
  }

  async getRule(ruleId: string): Promise<RuleOut | null> {
    const rule = this.store.rules.find((r) => r.id === ruleId);
    return rule === undefined ? null : structuredClone(rule);
  }

  async saveRule(rule: RuleOut): Promise<RuleOut> {
    const idx = this.store.rules.findIndex((r) => r.id === rule.id);
    if (idx >= 0) {
      this.store.rules[idx] = structuredClone(rule);
    } else {
      this.store.rules.push(structuredClone(rule));
      this.store.rule_last_fired_at[rule.id] = null;
    }
    return structuredClone(rule);
  }

  async deleteRule(ruleId: string): Promise<void> {
    this.store.rules = this.store.rules.filter((r) => r.id !== ruleId);
    delete this.store.rule_last_fired_at[ruleId];
  }

  async deleteTimeConditionTemplate(templateId: string): Promise<void> {
    this.store.time_condition_templates = this.store.time_condition_templates.filter(
      (t) => t.template_id !== templateId,
    );
  }

  async setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut> {
    const rule = this.store.rules.find((r) => r.id === ruleId);
    if (rule === undefined) {
      throw new Error(`Expected rule id, got unknown ${ruleId}`);
    }
    rule.enabled = enabled;
    return structuredClone(rule);
  }

  async getSettingsLocation(): Promise<SettingsLocationOut> {
    return structuredClone(this.store.settings_location);
  }

  async getSmtpConfig(): Promise<SmtpConfigOut | null> {
    const cfg = this.store.smtp_config;
    if (cfg === null) {
      return null;
    }
    return {
      host: cfg.host,
      port: cfg.port,
      username: cfg.username,
      mail_domain: cfg.mail_domain,
      from_address: cfg.from_address,
      password_configured: cfg.password.length > 0,
      last_test_recipient: this.store.smtp_last_test_recipient,
    };
  }

  async getVacationModeSettings(): Promise<VacationModeSettingsStatusOut> {
    return structuredClone(this.store.vacation_mode);
  }

  async saveSettingsLocation(
    location: SettingsLocationOut,
  ): Promise<SettingsLocationOut> {
    this.store.settings_location = structuredClone(location);
    return structuredClone(location);
  }

  async saveTimeConditionTemplate(
    template: TimeConditionTemplateOut,
  ): Promise<TimeConditionTemplateOut> {
    const idx = this.store.time_condition_templates.findIndex(
      (t) => t.template_id === template.template_id,
    );
    if (idx >= 0) {
      this.store.time_condition_templates[idx] = structuredClone(template);
    } else {
      this.store.time_condition_templates.push(structuredClone(template));
    }
    return structuredClone(template);
  }

  async resetSmtpConfig(): Promise<void> {
    this.store.smtp_config = null;
    this.store.smtp_last_test_recipient = null;
  }

  async saveSmtpConfig(config: SmtpConfigIn): Promise<SmtpConfigOut> {
    const existing = this.store.smtp_config;
    const password =
      config.password ?? existing?.password ?? "";
    this.store.smtp_config = {
      host: config.host,
      port: config.port,
      username: config.username,
      password,
      mail_domain: config.mail_domain,
      from_address: config.from_address,
    };
    const out = await this.getSmtpConfig();
    if (out === null) {
      throw new Error("Expected SMTP config after save, got null");
    }
    return out;
  }

  async sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut> {
    if (input.host.trim() === "") {
      return { ok: false, message: "Expected SMTP host, got empty value" };
    }
    if (input.to_address.trim() === "") {
      return { ok: false, message: "Expected recipient email, got empty value" };
    }
    this.store.smtp_last_test_recipient = input.to_address.trim();
    return {
      ok: true,
      message: `Test email queued to ${input.to_address.trim()} (mock — no message sent)`,
    };
  }

  async saveVacationModeSettings(
    settings: VacationModeSettingsOut,
  ): Promise<VacationModeSettingsStatusOut> {
    this.store.vacation_mode = {
      ...structuredClone(settings),
      armed: this.store.vacation_mode.armed,
    };
    return structuredClone(this.store.vacation_mode);
  }

  async sendVacationModeTestEmail(
    input: VacationModeTestEmailIn,
  ): Promise<VacationModeTestEmailOut> {
    return {
      ok: true,
      message: `Vacation mode ${input.kind} test email queued (mock — no message sent)`,
    };
  }

  async listActionDevices(): Promise<RuleActionDeviceOut[]> {
    try {
      const state = await api.fetchState();
      const devices: RuleActionDeviceOut[] = [];
      for (const family of state.families) {
        if (family.id !== "kasa" && family.id !== "tailwind") {
          continue;
        }
        for (const device of family.devices) {
          if (device.kind === "switch" || device.kind === "door") {
            devices.push({
              family_id: family.id,
              device_id: device.id,
              label: device.label,
              kind: device.kind,
            });
          }
        }
      }
      if (devices.length > 0) {
        return devices;
      }
    } catch {
      // Discovery incomplete or API unavailable — fall back to fixtures.
    }
    return structuredClone(this.store.action_devices);
  }
}

/** Rules list reads ``automation-rules.json``; roster/geofences/mail use live HTTP when available. */
class RulesDataSourceWithHttpSettings implements RulesDataSource {
  private cachedRules: RuleOut[] | null = null;

  private cachedSettingsLocation: SettingsLocationOut | null = null;

  private rulesFileBacked = false;

  constructor(
    private readonly inner: MockRulesDataSource,
    private readonly rulesLive: boolean,
  ) {}

  private assertRulesMutable(): void {
    if (this.rulesFileBacked) {
      throw new Error(FILE_BACKED_RULES_ERROR);
    }
  }

  private async loadFileBackedRules(): Promise<RuleOut[] | null> {
    if (this.cachedRules !== null) {
      return this.cachedRules;
    }
    try {
      const live = await api.fetchRules();
      if (live.length > 0) {
        this.cachedRules = live;
        this.rulesFileBacked = true;
        return live;
      }
    } catch (err) {
      console.warn("Automation rules bundle fetch failed", err);
    }
    return null;
  }

  deleteGeofence(geofenceId: string): Promise<void> {
    if (this.rulesLive) {
      return api.deleteRulesGeofence(geofenceId);
    }
    return this.inner.deleteGeofence(geofenceId);
  }

  deleteUser(userId: string): Promise<void> {
    return this.inner.deleteUser(userId);
  }

  deleteRule(ruleId: string): Promise<void> {
    this.assertRulesMutable();
    return this.inner.deleteRule(ruleId);
  }

  deleteTimeConditionTemplate(templateId: string): Promise<void> {
    return this.inner.deleteTimeConditionTemplate(templateId);
  }

  getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return api.fetchMyTracksGeofencesSync();
  }

  getMyTracksUsersSync(): Promise<MyTracksUsersSyncOut> {
    return api.fetchMyTracksUsersSync();
  }

  getMyTracksSettings(): Promise<MyTracksSettingsOut | null> {
    return api.fetchMyTracksSettings();
  }

  async getRule(ruleId: string): Promise<RuleOut | null> {
    const rules = await this.loadFileBackedRules();
    if (rules !== null) {
      const rule = rules.find((row) => row.id === ruleId);
      return rule === undefined ? null : structuredClone(rule);
    }
    return this.inner.getRule(ruleId);
  }

  async getSettingsLocation(): Promise<SettingsLocationOut> {
    if (this.cachedSettingsLocation !== null) {
      return structuredClone(this.cachedSettingsLocation);
    }
    if (this.rulesLive) {
      try {
        const live = await api.fetchRulesSettingsLocation();
        this.cachedSettingsLocation = live;
        return structuredClone(live);
      } catch (err) {
        console.warn("Automation rules settings location fetch failed", err);
      }
    }
    return this.inner.getSettingsLocation();
  }

  getSmtpConfig(): Promise<SmtpConfigOut | null> {
    return api.fetchSmtpConfig();
  }

  async getVacationModeSettings(): Promise<VacationModeSettingsStatusOut> {
    if (this.rulesLive) {
      try {
        return await api.fetchVacationModeSettings();
      } catch (err) {
        console.warn("Vacation mode settings fetch failed", err);
      }
    }
    return this.inner.getVacationModeSettings();
  }

  async getStatus(): Promise<RulesStatusOut> {
    try {
      const status = await api.fetchRulesStatus();
      await this.loadFileBackedRules();
      return status;
    } catch (err) {
      console.warn("Rules status fetch failed", err);
      return this.inner.getStatus();
    }
  }

  isRulesFileBacked(): boolean {
    return this.rulesFileBacked;
  }

  listActionDevices(): Promise<RuleActionDeviceOut[]> {
    return this.inner.listActionDevices();
  }

  async listGeofences(): Promise<GeofenceOut[]> {
    if (this.rulesLive) {
      return await api.fetchRulesGeofences();
    }
    return this.inner.listGeofences();
  }

  async listUserStatus(): Promise<UserStatusOut[]> {
    if (this.rulesLive) {
      return await api.fetchRulesUserStatus();
    }
    return this.inner.listUserStatus();
  }

  async listUsers(): Promise<UserOut[]> {
    if (this.rulesLive) {
      return await api.fetchRulesUsers();
    }
    return this.inner.listUsers();
  }

  async listUserObservedWifi(userId: string): Promise<ObservedWifiNetworkOut[]> {
    if (this.rulesLive) {
      return await api.fetchUserObservedWifi(userId);
    }
    return this.inner.listUserObservedWifi(userId);
  }

  async listRules(): Promise<RuleOut[]> {
    const rules = await this.loadFileBackedRules();
    if (rules !== null) {
      return structuredClone(rules);
    }
    return this.inner.listRules();
  }

  listTimeConditionTemplates(): Promise<TimeConditionTemplateOut[]> {
    return this.inner.listTimeConditionTemplates();
  }

  resetMyTracksSettings(): Promise<void> {
    return api.clearMyTracksSettings();
  }

  resetSmtpConfig(): Promise<void> {
    return api.clearSmtpConfig();
  }

  saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut> {
    if (this.rulesLive) {
      return api.putRulesGeofence(geofence);
    }
    return this.inner.saveGeofence(geofence);
  }

  saveMyTracksSettings(config: MyTracksSettingsIn): Promise<MyTracksSettingsOut> {
    return api.putMyTracksSettings(config);
  }

  saveUser(user: UserOut): Promise<UserOut> {
    return this.inner.saveUser(user);
  }

  setUserHomeWifi(
    userId: string,
    homeWifi: UserHomeWifiIn,
  ): Promise<UserOut> {
    if (this.rulesLive) {
      return api.putUserHomeWifi(userId, homeWifi);
    }
    return this.inner.setUserHomeWifi(userId, homeWifi);
  }

  saveRule(rule: RuleOut): Promise<RuleOut> {
    this.assertRulesMutable();
    return this.inner.saveRule(rule);
  }

  async saveSettingsLocation(
    location: SettingsLocationOut,
  ): Promise<SettingsLocationOut> {
    if (this.rulesLive) {
      const saved = await api.putRulesSettingsLocation(location);
      this.cachedSettingsLocation = saved;
      return structuredClone(saved);
    }
    return this.inner.saveSettingsLocation(location);
  }

  saveSmtpConfig(config: SmtpConfigIn): Promise<SmtpConfigOut> {
    return api.putSmtpConfig(config);
  }

  saveTimeConditionTemplate(
    template: TimeConditionTemplateOut,
  ): Promise<TimeConditionTemplateOut> {
    return this.inner.saveTimeConditionTemplate(template);
  }

  sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut> {
    return api.sendSmtpTestEmail(input);
  }

  async saveVacationModeSettings(
    settings: VacationModeSettingsOut,
  ): Promise<VacationModeSettingsStatusOut> {
    if (this.rulesLive) {
      return api.putVacationModeSettings(settings);
    }
    return this.inner.saveVacationModeSettings(settings);
  }

  sendVacationModeTestEmail(
    input: VacationModeTestEmailIn,
  ): Promise<VacationModeTestEmailOut> {
    if (this.rulesLive) {
      return api.sendVacationModeTestEmail(input);
    }
    return this.inner.sendVacationModeTestEmail(input);
  }

  setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut> {
    this.assertRulesMutable();
    return this.inner.setRuleEnabled(ruleId, enabled);
  }

  syncGeofencesFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut> {
    return api.syncMyTracksGeofences(credentials);
  }

  syncUsersFromMyTracks(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksUsersSyncOut> {
    return api.syncMyTracksUsers(credentials);
  }
}

async function rulesApiAvailable(): Promise<boolean> {
  try {
    const res = await fetch("/v1/rules/geofences", { headers: authHeaders() });
    return res.ok || res.status === 401;
  } catch {
    return false;
  }
}

async function settingsApiAvailable(): Promise<boolean> {
  try {
    const headers = authHeaders();
    const [smtp, myTracks] = await Promise.all([
      fetch("/v1/settings/smtp", { headers }),
      fetch("/v1/settings/my-tracks", { headers }),
    ]);
    const smtpOk = smtp.ok || smtp.status === 401;
    const myTracksOk = myTracks.ok || myTracks.status === 401;
    return smtpOk && myTracksOk;
  } catch {
    return false;
  }
}

export async function createRulesDataSource(): Promise<RulesDataSource> {
  const mock = new MockRulesDataSource();
  const [settingsLive, rulesLive] = await Promise.all([
    settingsApiAvailable(),
    rulesApiAvailable(),
  ]);
  if (settingsLive) {
    return new RulesDataSourceWithHttpSettings(mock, rulesLive);
  }
  return mock;
}
