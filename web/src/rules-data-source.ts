// Rules hub data access — mock store for Phase 1; HTTP implementation in Phase 2.

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
  MyTracksParticipantsSyncOut,
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

declare const DOMESTI_RULES_FORCE_MOCK: boolean | undefined;

export interface RulesDataSource {
  isMock(): boolean;
  getStatus(): Promise<RulesStatusOut>;
  listGeofences(): Promise<GeofenceOut[]>;
  saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut>;
  deleteGeofence(geofenceId: string): Promise<void>;
  deleteParticipant(participantId: string): Promise<void>;
  getMyTracksParticipantsSync(): Promise<MyTracksParticipantsSyncOut>;
  listParticipants(): Promise<ParticipantOut[]>;
  saveParticipant(participant: ParticipantOut): Promise<ParticipantOut>;
  syncParticipantsFromMyTracks(): Promise<MyTracksParticipantsSyncOut>;
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

  async syncParticipantsFromMyTracks(): Promise<MyTracksParticipantsSyncOut> {
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

export async function createRulesDataSource(): Promise<RulesDataSource> {
  if (typeof DOMESTI_RULES_FORCE_MOCK !== "undefined" && DOMESTI_RULES_FORCE_MOCK) {
    return new MockRulesDataSource();
  }
  try {
    const res = await fetch("/v1/rules/status", { headers: authHeaders() });
    if (res.ok) {
      // HttpRulesDataSource lands in Phase 2 wire-up PR.
      return new MockRulesDataSource();
    }
  } catch {
    // Server down or route missing.
  }
  return new MockRulesDataSource();
}
