// Rules hub data access — mock store for Phase 1; HTTP implementation in Phase 2.

import { api, authHeaders } from "./api.js";
import {
  createMockStoreSeed,
  haversineM,
  mockSunRow,
  type MockStoreSeed,
} from "./rules-mock-fixtures.js";
import type {
  GeofenceOut,
  ParticipantOut,
  ParticipantStatusOut,
  RuleActionDeviceOut,
  RuleOut,
  RulesStatusOut,
  SettingsLocationOut,
} from "./types.js";

declare const DOMESTI_RULES_FORCE_MOCK: boolean | undefined;

export interface RulesDataSource {
  isMock(): boolean;
  getStatus(): Promise<RulesStatusOut>;
  listGeofences(): Promise<GeofenceOut[]>;
  saveGeofence(geofence: GeofenceOut): Promise<GeofenceOut>;
  deleteGeofence(geofenceId: string): Promise<void>;
  listParticipants(): Promise<ParticipantOut[]>;
  saveParticipant(participant: ParticipantOut): Promise<ParticipantOut>;
  deleteParticipant(participantId: string): Promise<void>;
  listRules(): Promise<RuleOut[]>;
  getRule(ruleId: string): Promise<RuleOut | null>;
  saveRule(rule: RuleOut): Promise<RuleOut>;
  deleteRule(ruleId: string): Promise<void>;
  setRuleEnabled(ruleId: string, enabled: boolean): Promise<RuleOut>;
  getSettingsLocation(): Promise<SettingsLocationOut>;
  saveSettingsLocation(location: SettingsLocationOut): Promise<SettingsLocationOut>;
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

function evaluateRuleConditionsMet(rule: RuleOut, store: MockStoreSeed): boolean {
  if (!rule.enabled) {
    return false;
  }
  for (const condition of rule.conditions.all) {
    if (condition.type === "after_sunset") {
      if (!mockSunRow().is_dark) {
        return false;
      }
      continue;
    }
    if (condition.type === "before_sunrise") {
      if (mockSunRow().is_dark) {
        return false;
      }
      continue;
    }
    const geofence = store.geofences.find(
      (g) => g.geofence_id === condition.geofence_id,
    );
    if (geofence === undefined) {
      return false;
    }
    for (const participantId of condition.participant_ids) {
      const fix = store.participant_fixes[participantId] ?? null;
      const inside = participantInsideGeofence(fix, geofence);
      if (condition.type === "participants_inside_geofence" && !inside) {
        return false;
      }
      if (condition.type === "participants_outside_geofence" && inside) {
        return false;
      }
    }
  }
  return true;
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
      rules: this.store.rules.map((rule) => ({
        id: rule.id,
        label: rule.label,
        enabled: rule.enabled,
        condition_currently_true: evaluateRuleConditionsMet(rule, this.store),
        last_fired_at: this.store.rule_last_fired_at[rule.id] ?? null,
        last_error: null,
      })),
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

  async deleteParticipant(participantId: string): Promise<void> {
    this.store.participants = this.store.participants.filter(
      (p) => p.participant_id !== participantId,
    );
    delete this.store.participant_fixes[participantId];
  }

  async listRules(): Promise<RuleOut[]> {
    return structuredClone(this.store.rules);
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

  async saveSettingsLocation(
    location: SettingsLocationOut,
  ): Promise<SettingsLocationOut> {
    this.store.settings_location = structuredClone(location);
    return structuredClone(location);
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
