// Shared rule condition evaluation for mock data source and Status tab display.

import { haversineM, mockSunRow, type MockStoreSeed } from "./rules-mock-fixtures.js";
import type {
  GeofenceOut,
  ParticipantFixOut,
  RuleConditionOut,
  RuleConditionStatusOut,
  RuleOut,
} from "./types.js";

export const DEFAULT_MIN_FIX_ACCURACY_M = 50;

function formatHhmmDisplay(hhmm: string): string {
  const parsed = parseHhmm(hhmm);
  if (parsed === null) {
    return hhmm;
  }
  const h = Math.floor(parsed / 60);
  const m = parsed % 60;
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

function localMinutesNow(): number {
  const now = new Date();
  return now.getHours() * 60 + now.getMinutes();
}

function parseHhmm(hhmm: string): number | null {
  const match = /^(\d{1,2}):(\d{2})$/.exec(hhmm.trim());
  if (match === null) {
    return null;
  }
  const h = Number(match[1]);
  const m = Number(match[2]);
  if (h < 0 || h > 23 || m < 0 || m > 59) {
    return null;
  }
  return h * 60 + m;
}

export interface RuleEvaluationOut {
  all_met: boolean;
  conditions: RuleConditionStatusOut[];
}

function fixUsableForRule(
  fix: ParticipantFixOut | null,
  minAccuracyM: number,
): boolean {
  if (fix === null) {
    return false;
  }
  if (fix.accuracy_m !== null && fix.accuracy_m > minAccuracyM) {
    return false;
  }
  return true;
}

function geofenceLabel(store: MockStoreSeed, geofenceId: string): string {
  return store.geofences.find((g) => g.geofence_id === geofenceId)?.label ?? geofenceId;
}

function participantDisplayName(store: MockStoreSeed, participantId: string): string {
  return (
    store.participants.find((p) => p.participant_id === participantId)?.display_name
    ?? participantId
  );
}

function participantInsideGeofence(
  fix: ParticipantFixOut | null,
  geofence: GeofenceOut,
  minAccuracyM: number,
): boolean {
  if (!fixUsableForRule(fix, minAccuracyM)) {
    return false;
  }
  if (!geofence.enabled || fix === null) {
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

function evaluateCondition(
  condition: RuleConditionOut,
  rule: RuleOut,
  store: MockStoreSeed,
): RuleConditionStatusOut {
  const minAccuracyM = rule.min_fix_accuracy_m;
  if (condition.type === "after_sunset") {
    const sun = mockSunRow();
    const met = sun.is_dark;
    return {
      condition,
      label: "After sunset",
      met,
      detail: met
        ? `Dark now (sunset ${new Date(sun.sunset_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})`
        : `Still light until sunset (${new Date(sun.sunset_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})`,
    };
  }
  if (condition.type === "before_sunrise") {
    const sun = mockSunRow();
    const met = !sun.is_dark;
    return {
      condition,
      label: "Before sunrise",
      met,
      detail: met
        ? `Daytime until sunrise (${new Date(sun.sunrise_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})`
        : `After sunrise window (sunrise was ${new Date(sun.sunrise_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })})`,
    };
  }
  if (condition.type === "after_local_time") {
    const target = parseHhmm(condition.time_hhmm);
    const now = localMinutesNow();
    const met = target !== null && now >= target;
    return {
      condition,
      label: `After ${formatHhmmDisplay(condition.time_hhmm)}`,
      met,
      detail:
        target === null
          ? `Invalid time ${condition.time_hhmm}`
          : met
            ? `Local time is past ${formatHhmmDisplay(condition.time_hhmm)}`
            : `Waiting until ${formatHhmmDisplay(condition.time_hhmm)}`,
    };
  }
  if (condition.type === "before_local_time") {
    const target = parseHhmm(condition.time_hhmm);
    const now = localMinutesNow();
    const met = target !== null && now < target;
    return {
      condition,
      label: `Before ${formatHhmmDisplay(condition.time_hhmm)}`,
      met,
      detail:
        target === null
          ? `Invalid time ${condition.time_hhmm}`
          : met
            ? `Local time is before ${formatHhmmDisplay(condition.time_hhmm)}`
            : `Past ${formatHhmmDisplay(condition.time_hhmm)} for today`,
    };
  }

  const geofence = store.geofences.find((g) => g.geofence_id === condition.geofence_id);
  const fenceLabel = geofenceLabel(store, condition.geofence_id);
  if (geofence === undefined) {
    return {
      condition,
      label: "Geofence",
      met: false,
      detail: `Unknown geofence "${condition.geofence_id}"`,
    };
  }

  const unmetNames: string[] = [];
  const ignoredAccuracy: string[] = [];
  for (const participantId of condition.participant_ids) {
    const fix = store.participant_fixes[participantId] ?? null;
    const name = participantDisplayName(store, participantId);
    if (fix !== null && !fixUsableForRule(fix, minAccuracyM)) {
      ignoredAccuracy.push(
        `${name} (±${fix.accuracy_m ?? "?"} m > ${minAccuracyM} m threshold)`,
      );
      unmetNames.push(name);
      continue;
    }
    const inside = participantInsideGeofence(fix, geofence, minAccuracyM);
    if (condition.type === "participants_inside_geofence" && !inside) {
      unmetNames.push(name);
    }
    if (condition.type === "participants_outside_geofence" && inside) {
      unmetNames.push(name);
    }
  }

  const met = unmetNames.length === 0;
  const wantInside = condition.type === "participants_inside_geofence";
  const label = wantInside
    ? `Inside ${fenceLabel}`
    : `Outside ${fenceLabel}`;
  let detail: string;
  if (met) {
    detail = wantInside
      ? `All selected participants inside ${fenceLabel}`
      : `All selected participants outside ${fenceLabel}`;
  } else if (ignoredAccuracy.length > 0) {
    detail = `Ignored low-accuracy fix: ${ignoredAccuracy.join("; ")}`;
  } else {
    detail = wantInside
      ? `Waiting for: ${unmetNames.join(", ")}`
      : `Still inside ${fenceLabel}: ${unmetNames.join(", ")}`;
  }
  return { condition, label, met, detail };
}

export function evaluateRule(
  rule: RuleOut,
  store: MockStoreSeed,
): RuleEvaluationOut {
  const conditions = rule.conditions.all.map((condition) =>
    evaluateCondition(condition, rule, store),
  );
  return {
    all_met: rule.enabled && conditions.every((c) => c.met),
    conditions,
  };
}

export function evaluateRuleConditionsMet(
  rule: RuleOut,
  store: MockStoreSeed,
): boolean {
  return evaluateRule(rule, store).all_met;
}
