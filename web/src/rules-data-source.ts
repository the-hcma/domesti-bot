// Rules hub data access — mock store for rules; SMTP uses the HTTP API when available.

import { api, authHeaders } from "./api.js";
import {
  createMockStoreSeed,
  haversineM,
  mockSunRow,
  type MockStoreSeed,
} from "./rules-mock-fixtures.js";
import { evaluateRule } from "./rules-evaluate.js";
import type {
  GeofenceOut,
  MyTracksGeofencesSyncOut,
  MyTracksParticipantsSyncOut,
  MyTracksSettingsIn,
  MyTracksSettingsOut,
  MyTracksSyncIn,
  ParticipantOut,
  ParticipantStatusOut,
  RuleActionDeviceOut,
  RuleOut,
  RulesStatusOut,
  SettingsLocationOut,
  SmtpConfigIn,
  SmtpConfigOut,
  SmtpTestEmailIn,
  SmtpTestEmailOut,
  TimeConditionTemplateOut,
} from "./types.js";

export interface RulesDataSource {
  isMailLive(): boolean;
  isMock(): boolean;
  getStatus(): Promise<RulesStatusOut>;
  listGeofences(): Promise<GeofenceOut[]>;
  saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut>;
  deleteGeofence(geofenceId: string): Promise<void>;
  deleteParticipant(participantId: string): Promise<void>;
  getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut>;
  getMyTracksParticipantsSync(): Promise<MyTracksParticipantsSyncOut>;
  getMyTracksSettings(): Promise<MyTracksSettingsOut | null>;
  listParticipants(): Promise<ParticipantOut[]>;
  resetMyTracksSettings(): Promise<void>;
  saveMyTracksSettings(config: MyTracksSettingsIn): Promise<MyTracksSettingsOut>;
  saveParticipant(participant: ParticipantOut): Promise<ParticipantOut>;
  syncGeofencesFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut>;
  syncParticipantsFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksParticipantsSyncOut>;
  listRules(): Promise<RuleOut[]>;
  listTimeConditionTemplates(): Promise<TimeConditionTemplateOut[]>;
  getRule(ruleId: string): Promise<RuleOut | null>;
  saveRule(rule: RuleOut): Promise<RuleOut>;
  deleteRule(ruleId: string): Promise<void>;
  deleteTimeConditionTemplate(templateId: string): Promise<void>;
  setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut>;
  getSettingsLocation(): Promise<SettingsLocationOut>;
  getSmtpConfig(): Promise<SmtpConfigOut | null>;
  saveSettingsLocation(location: SettingsLocationOut): Promise<SettingsLocationOut>;
  resetSmtpConfig(): Promise<void>;
  saveSmtpConfig(config: SmtpConfigIn): Promise<SmtpConfigOut>;
  sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut>;
  saveTimeConditionTemplate(
    template: TimeConditionTemplateOut,
  ): Promise<TimeConditionTemplateOut>;
  listActionDevices(): Promise<RuleActionDeviceOut[]>;
}

function resolveMyTracksPassword(
  store: MockStoreSeed,
  credentials: MyTracksSyncIn | undefined,
): string {
  if (credentials?.password !== undefined && credentials.password !== "") {
    return credentials.password;
  }
  return store.my_tracks_settings?.password ?? "";
}

function requireMyTracksDomain(store: MockStoreSeed): string {
  const domain = store.my_tracks_settings?.domain.trim() ?? "";
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

function participantInsideGeofence(
  fix: { lat: number; lon: number } | null,
  geofence: GeofenceOut,
): boolean {
  if (fix === null || !geofence.enabled) {
    return false;
  }
  const dist = haversineM(
    fix.lat,
    fix.lon,
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

  isMailLive(): boolean {
    return false;
  }

  isMock(): boolean {
    return true;
  }

  async getStatus(): Promise<RulesStatusOut> {
    const now = Date.now();
    const participants: ParticipantStatusOut[] = this.store.participants.map(
      (p) => {
        const fix = this.store.participant_fixes[p.participant_id] ?? null;
        const age_seconds =
          fix === null
            ? null
            : Math.max(0, Math.floor((now - Date.parse(fix.received_at)) / 1000));
        const inside_geofence_ids = this.store.geofences
          .filter((g) => participantInsideGeofence(fix, g))
          .map((g) => g.geofence_id);
        return {
          ...p,
          last_fix: fix,
          inside_geofence_ids,
          age_seconds,
        };
      },
    );
    const sun = mockSunRow();
    return {
      participants,
      geofences: structuredClone(this.store.geofences),
      rules: this.store.rules.map((rule) => {
        const evaluation = evaluateRule(rule, this.store);
        return {
          id: rule.id,
          label: rule.label,
          enabled: rule.enabled,
          condition_currently_true: evaluation.all_met,
          conditions: evaluation.conditions,
          last_fired_at: this.store.rule_last_fired_at[rule.id] ?? null,
          last_error: null,
        };
      }),
      sun,
      evaluator: {
        last_run_at: new Date(now - 15_000).toISOString(),
        next_sun_check_at: new Date(now + 45_000).toISOString(),
      },
      using_mock: true,
    };
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
      rule.conditions.all.some(
        (c) =>
          (c.type === "participants_inside_geofence" ||
            c.type === "participants_outside_geofence") &&
          c.geofence_id === geofenceId,
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

  async deleteParticipant(participantId: string): Promise<void> {
    this.store.participants = this.store.participants.filter(
      (p) => p.participant_id !== participantId,
    );
    delete this.store.participant_fixes[participantId];
  }

  async getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return {
      source: "my-tracks",
      last_synced_at: this.store.geofences_sync.last_synced_at,
      geofence_count: this.store.geofences.length,
    };
  }

  async getMyTracksParticipantsSync(): Promise<MyTracksParticipantsSyncOut> {
    return {
      source: "my-tracks",
      last_synced_at: this.store.participants_sync.last_synced_at,
      participant_count: this.store.participants.length,
      webhook_ready: true,
    };
  }

  async listParticipants(): Promise<ParticipantOut[]> {
    return structuredClone(this.store.participants);
  }

  async saveParticipant(participant: ParticipantOut): Promise<ParticipantOut> {
    const idx = this.store.participants.findIndex(
      (p) => p.participant_id === participant.participant_id,
    );
    if (idx >= 0) {
      this.store.participants[idx] = structuredClone(participant);
    } else {
      this.store.participants.push(structuredClone(participant));
    }
    return structuredClone(participant);
  }

  async getMyTracksSettings(): Promise<MyTracksSettingsOut | null> {
    const cfg = this.store.my_tracks_settings;
    if (cfg === null) {
      return null;
    }
    return {
      domain: cfg.domain,
      username: cfg.username,
      password_configured: cfg.password.length > 0,
    };
  }

  async resetMyTracksSettings(): Promise<void> {
    this.store.my_tracks_settings = null;
  }

  async saveMyTracksSettings(
    config: MyTracksSettingsIn,
  ): Promise<MyTracksSettingsOut> {
    const existing = this.store.my_tracks_settings;
    const password =
      config.password ?? existing?.password ?? "";
    this.store.my_tracks_settings = {
      domain: config.domain.trim(),
      username: config.username.trim(),
      password,
    };
    const saved = await this.getMyTracksSettings();
    if (saved === null) {
      throw new Error("Expected My Tracks settings after save, got null");
    }
    return saved;
  }

  async syncGeofencesFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut> {
    requireMyTracksDomain(this.store);
    const password = resolveMyTracksPassword(this.store, credentials);
    if (password === "") {
      throw new Error("Expected My Tracks admin password, got empty value");
    }
    this.store.geofences = structuredClone(this.store.my_tracks_geofence_catalog);
    this.store.geofences_sync.last_synced_at = new Date().toISOString();
    return this.getMyTracksGeofencesSync();
  }

  async syncParticipantsFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksParticipantsSyncOut> {
    requireMyTracksDomain(this.store);
    const password = resolveMyTracksPassword(this.store, credentials);
    if (password === "") {
      throw new Error("Expected My Tracks admin password, got empty value");
    }
    this.store.participants = structuredClone(this.store.my_tracks_participant_catalog);
    this.store.participants_sync.last_synced_at = new Date().toISOString();
    return this.getMyTracksParticipantsSync();
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

/** Rules stay mock-backed; SMTP and My Tracks settings use live HTTP APIs when available. */
class RulesDataSourceWithHttpSettings implements RulesDataSource {
  constructor(private readonly inner: MockRulesDataSource) {}

  deleteGeofence(geofenceId: string): Promise<void> {
    return this.inner.deleteGeofence(geofenceId);
  }

  deleteParticipant(participantId: string): Promise<void> {
    return this.inner.deleteParticipant(participantId);
  }

  deleteRule(ruleId: string): Promise<void> {
    return this.inner.deleteRule(ruleId);
  }

  deleteTimeConditionTemplate(templateId: string): Promise<void> {
    return this.inner.deleteTimeConditionTemplate(templateId);
  }

  getMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return api.fetchMyTracksGeofencesSync();
  }

  getMyTracksParticipantsSync(): Promise<MyTracksParticipantsSyncOut> {
    return api.fetchMyTracksParticipantsSync();
  }

  getMyTracksSettings(): Promise<MyTracksSettingsOut | null> {
    return api.fetchMyTracksSettings();
  }

  getRule(ruleId: string): Promise<RuleOut | null> {
    return this.inner.getRule(ruleId);
  }

  getSettingsLocation(): Promise<SettingsLocationOut> {
    return this.inner.getSettingsLocation();
  }

  getSmtpConfig(): Promise<SmtpConfigOut | null> {
    return api.fetchSmtpConfig();
  }

  getStatus(): Promise<RulesStatusOut> {
    return this.inner.getStatus();
  }

  isMailLive(): boolean {
    return true;
  }

  isMock(): boolean {
    return this.inner.isMock();
  }

  listActionDevices(): Promise<RuleActionDeviceOut[]> {
    return this.inner.listActionDevices();
  }

  listGeofences(): Promise<GeofenceOut[]> {
    return this.inner.listGeofences();
  }

  listParticipants(): Promise<ParticipantOut[]> {
    return this.inner.listParticipants();
  }

  listRules(): Promise<RuleOut[]> {
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
    return this.inner.saveGeofence(geofence);
  }

  saveMyTracksSettings(config: MyTracksSettingsIn): Promise<MyTracksSettingsOut> {
    return api.putMyTracksSettings(config);
  }

  saveParticipant(participant: ParticipantOut): Promise<ParticipantOut> {
    return this.inner.saveParticipant(participant);
  }

  saveRule(rule: RuleOut): Promise<RuleOut> {
    return this.inner.saveRule(rule);
  }

  saveSettingsLocation(location: SettingsLocationOut): Promise<SettingsLocationOut> {
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

  setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut> {
    return this.inner.setRuleEnabled(ruleId, enabled);
  }

  syncGeofencesFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksGeofencesSyncOut> {
    return api.syncMyTracksGeofences(credentials);
  }

  syncParticipantsFromMyTracks(
    credentials?: MyTracksSyncIn,
  ): Promise<MyTracksParticipantsSyncOut> {
    return api.syncMyTracksParticipants(credentials);
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
  if (await settingsApiAvailable()) {
    return new RulesDataSourceWithHttpSettings(mock);
  }
  return mock;
}
