// Human-readable Automations rule summaries for the Rules tab.

import {
  ALL_DAYS_OF_WEEK,
  DAY_OF_WEEK_LABELS,
  WEEKDAY_DAYS,
  WEEKEND_DAYS,
} from "./rules-ui-helpers.js";
import {
  Ep1ReadingMetric,
  RuleActionType,
  RuleConditionType,
  type GeofenceOut,
  type RuleActionDeviceOut,
  type RuleConditionOut,
  type RuleOut,
  type UserOut,
} from "./types.js";

export interface RuleSummaryContext {
  deviceLabelByKey: ReadonlyMap<string, string>;
  geofenceLabelById: ReadonlyMap<string, string>;
  userDisplayNameById: ReadonlyMap<string, string>;
}

export interface RuleSummarySections {
  /** Presence / geofence requirements (who must be where). */
  presence: string[];
  /** Clock and astronomical time windows. */
  timing: string[];
  /** Device power-state requirements. */
  devices: string[];
  /** Device commands when the rule fires. */
  actions: string[];
}

function deviceKey(familyId: string, deviceId: string): string {
  return `${familyId}\0${deviceId}`;
}

const IPV4_RE = /^\d{1,3}(?:\.\d{1,3}){3}$/;

function looksLikeIpv4(value: string): boolean {
  return IPV4_RE.test(value.trim());
}

export function referencesGeofenceId(
  condition: RuleConditionOut,
  geofenceId: string,
): boolean {
  if (
    condition.type === RuleConditionType.UsersInsideGeofence
    || condition.type === RuleConditionType.UsersInsideGeofenceForS
    || condition.type === RuleConditionType.UsersOutsideGeofence
    || condition.type === RuleConditionType.UsersOutsideGeofenceForS
  ) {
    return condition.geofence_id === geofenceId;
  }
  if (condition.type === RuleConditionType.All || condition.type === RuleConditionType.Any) {
    return condition.conditions.some((child) =>
      referencesGeofenceId(child, geofenceId),
    );
  }
  return false;
}

export function formatDeviceDisplay(
  deviceId: string,
  displayName?: string | null,
): string {
  const trimmedId = deviceId.trim();
  const trimmedName = (displayName ?? "").trim();
  if (
    trimmedName !== ""
    && trimmedName.toLowerCase() !== trimmedId.toLowerCase()
  ) {
    return `${trimmedName} (${trimmedId})`;
  }
  return trimmedId;
}

export function resolveDeviceLabel(
  familyId: string,
  deviceId: string,
  context: RuleSummaryContext,
  snapshotDisplayName?: string | null,
): string {
  const key = deviceKey(familyId, deviceId);
  const direct = context.deviceLabelByKey.get(key);
  const snapshot = (snapshotDisplayName ?? "").trim();
  // Prefer a non-IPv4 roster label; otherwise prefer the rule snapshot.
  // Skip raw IPv4 roster labels so we do not present IP as a friendly name.
  if (direct !== undefined && direct !== "" && !looksLikeIpv4(direct)) {
    return formatDeviceDisplay(deviceId, direct);
  }
  if (snapshot !== "") {
    return formatDeviceDisplay(deviceId, snapshot);
  }
  return formatDeviceDisplay(deviceId, undefined);
}

export function buildRuleSummaryContext(
  users: readonly UserOut[],
  geofences: readonly GeofenceOut[],
  actionDevices: readonly RuleActionDeviceOut[],
): RuleSummaryContext {
  const userDisplayNameById = new Map<string, string>();
  for (const user of users) {
    userDisplayNameById.set(user.user_id, user.display_name);
  }
  const geofenceLabelById = new Map<string, string>();
  for (const geofence of geofences) {
    geofenceLabelById.set(geofence.geofence_id, geofence.label);
  }
  const deviceLabelByKey = new Map<string, string>();
  for (const device of actionDevices) {
    deviceLabelByKey.set(deviceKey(device.family_id, device.device_id), device.label);
  }
  return { deviceLabelByKey, geofenceLabelById, userDisplayNameById };
}

export function collectUserIdsFromConditions(
  conditions: readonly RuleConditionOut[],
): string[] {
  const ids = new Set<string>();
  const walk = (condition: RuleConditionOut): void => {
    if (
      condition.type === RuleConditionType.UsersInsideGeofence
      || condition.type === RuleConditionType.UsersInsideGeofenceForS
      || condition.type === RuleConditionType.UsersMinDistanceFromHomeM
      || condition.type === RuleConditionType.UsersOutsideGeofence
      || condition.type === RuleConditionType.UsersOutsideGeofenceForS
    ) {
      for (const userId of condition.user_ids) {
        ids.add(userId);
      }
      return;
    }
    if (condition.type === RuleConditionType.All || condition.type === RuleConditionType.Any) {
      for (const child of condition.conditions) {
        walk(child);
      }
    }
  };
  for (const condition of conditions) {
    walk(condition);
  }
  return [...ids].sort();
}

export function collectUserIdsFromRule(rule: RuleOut): string[] {
  return collectUserIdsFromConditions(rule.conditions.all);
}

export function joinNames(names: readonly string[]): string {
  if (names.length === 0) {
    return "nobody";
  }
  if (names.length === 1) {
    return names[0] ?? "nobody";
  }
  if (names.length === 2) {
    return `${names[0]} and ${names[1]}`;
  }
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

function formatLocalTime(hhmm: string): string {
  const match = /^(\d{1,2}):(\d{2})$/.exec(hhmm);
  if (match === null) {
    return hhmm;
  }
  let hour = Number(match[1]);
  const minute = match[2];
  const period = hour >= 12 ? "PM" : "AM";
  hour %= 12;
  if (hour === 0) {
    hour = 12;
  }
  return `${hour}:${minute} ${period}`;
}

function userDisplayNames(
  userIds: readonly string[],
  context: RuleSummaryContext,
): string[] {
  return userIds.map((id) => context.userDisplayNameById.get(id) ?? id);
}

function geofenceLabel(geofenceId: string, context: RuleSummaryContext): string {
  return context.geofenceLabelById.get(geofenceId) ?? geofenceId;
}

function formatDaysOfWeek(days: readonly number[]): string {
  const sorted = [...days].sort((a, b) => a - b);
  if (sorted.length === ALL_DAYS_OF_WEEK.length) {
    return "Every day";
  }
  const weekdaySet = new Set<number>(WEEKDAY_DAYS);
  const weekendSet = new Set<number>(WEEKEND_DAYS);
  if (
    sorted.length === WEEKDAY_DAYS.length
    && sorted.every((day) => weekdaySet.has(day))
  ) {
    return "Weekdays only";
  }
  if (
    sorted.length === WEEKEND_DAYS.length
    && sorted.every((day) => weekendSet.has(day))
  ) {
    return "Weekends only";
  }
  const labels = sorted.map((day) => DAY_OF_WEEK_LABELS[day] ?? String(day));
  return `On ${joinNames(labels)}`;
}

function formatDwellDuration(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds));
  if (whole < 60) {
    return `${whole} sec`;
  }
  const minutes = Math.floor(whole / 60);
  const remainder = whole % 60;
  if (remainder === 0) {
    return `${minutes} min`;
  }
  return `${minutes} min ${remainder} sec`;
}

function formatDwellNeed(minInsideS: number): string {
  return formatDwellDuration(minInsideS);
}

function formatDistanceMeters(distanceM: number): string {
  if (distanceM >= 1000) {
    const km = distanceM / 1000;
    if (Number.isInteger(km)) {
      return `${km} km`;
    }
    return `${km.toFixed(1)} km`;
  }
  if (Number.isInteger(distanceM)) {
    return `${distanceM} m`;
  }
  return `${Math.round(distanceM)} m`;
}

export function formatGeofenceDwellLabel(
  condition: Extract<RuleConditionOut, { type: typeof RuleConditionType.UsersInsideGeofenceForS }>,
  context: RuleSummaryContext,
): string {
  const names = userDisplayNames(condition.user_ids, context);
  const where = geofenceLabel(condition.geofence_id, context);
  const who = joinNames(names);
  const needLabel = formatDwellNeed(condition.min_inside_s);
  return `${who} inside ${where} for ${needLabel}+`;
}

export function formatGeofenceAwayDwellLabel(
  condition: Extract<RuleConditionOut, { type: typeof RuleConditionType.UsersOutsideGeofenceForS }>,
  context: RuleSummaryContext,
): string {
  const names = userDisplayNames(condition.user_ids, context);
  const where = geofenceLabel(condition.geofence_id, context);
  const who = joinNames(names);
  const needLabel = formatDwellNeed(condition.min_outside_s);
  return `${who} outside ${where} for ${needLabel}+`;
}

export function formatMinDistanceFromHomeLabel(
  condition: Extract<RuleConditionOut, { type: typeof RuleConditionType.UsersMinDistanceFromHomeM }>,
  context: RuleSummaryContext,
): string {
  const names = userDisplayNames(condition.user_ids, context);
  const who = joinNames(names);
  const needLabel = formatDistanceMeters(condition.min_distance_m);
  return `${who} ≥ ${needLabel} from home`;
}

export function formatPresenceEventLabel(
  condition: Extract<
    RuleConditionOut,
    { type: typeof RuleConditionType.UsersInsideGeofence | typeof RuleConditionType.UsersOutsideGeofence }
  >,
  context: RuleSummaryContext,
): string {
  const names = userDisplayNames(condition.user_ids, context);
  const where = geofenceLabel(condition.geofence_id, context);
  const who = joinNames(names);
  if (condition.type === RuleConditionType.UsersInsideGeofence) {
    return `When ${who} enter ${where}`;
  }
  return `When ${who} leave ${where}`;
}

export function formatTimingCondition(condition: RuleConditionOut): string | null {
  switch (condition.type) {
    case RuleConditionType.UsersInsideGeofence:
    case RuleConditionType.UsersInsideGeofenceForS:
    case RuleConditionType.UsersMinDistanceFromHomeM:
    case RuleConditionType.UsersOutsideGeofence:
    case RuleConditionType.UsersOutsideGeofenceForS:
    case RuleConditionType.DevicesAllInState:
    case RuleConditionType.DevicesAnyInState:
    case RuleConditionType.DevicesAnyInStateForS:
    case RuleConditionType.Ep1ReadingCompare:
      return null;
    case RuleConditionType.All:
    case RuleConditionType.Any:
      return null;
    case RuleConditionType.AfterSunset: {
      const offset = condition.offset_minutes;
      const start =
        offset > 0
          ? `At least ${offset} minute${offset === 1 ? "" : "s"} after sunset`
          : "After sunset";
      return `${start} until midnight`;
    }
    case RuleConditionType.BeforeSunrise: {
      const offset = condition.offset_minutes;
      const end =
        offset > 0
          ? `until ${offset} minute${offset === 1 ? "" : "s"} before sunrise`
          : "until sunrise";
      return `After midnight ${end}`;
    }
    case RuleConditionType.Daylight:
      return "During daylight (sunrise to sunset)";
    case RuleConditionType.AfterLocalTime:
      return `After ${formatLocalTime(condition.time_hhmm)}`;
    case RuleConditionType.BeforeLocalTime:
      return `Before ${formatLocalTime(condition.time_hhmm)}`;
    case RuleConditionType.LocalTimeWindow:
      return `Between ${formatLocalTime(condition.start_hhmm)} and ${formatLocalTime(condition.end_hhmm)}`;
    case RuleConditionType.DaysOfWeek:
      return formatDaysOfWeek(condition.days);
  }
}

export function formatDeviceStateCondition(
  condition: Extract<
    RuleConditionOut,
    {
      type:
        | typeof RuleConditionType.DevicesAllInState
        | typeof RuleConditionType.DevicesAnyInState
        | typeof RuleConditionType.DevicesAnyInStateForS;
    }
  >,
  context: RuleSummaryContext,
): string {
  const labels = condition.devices.map((entry) =>
    resolveDeviceLabel(
      entry.family_id,
      entry.device_id,
      context,
      entry.display_name,
    ),
  );
  const joined = joinNames(labels);
  if (condition.type === RuleConditionType.DevicesAnyInState) {
    return `Any of ${joined} is ${condition.state}`;
  }
  if (condition.type === RuleConditionType.DevicesAllInState) {
    return `All of ${joined} are ${condition.state}`;
  }
  const need = formatDwellDuration(condition.min_duration_s);
  return `Any of ${joined} ${condition.state} for ${need}+`;
}

export function formatEp1ReadingCompareCondition(
  condition: Extract<RuleConditionOut, { type: typeof RuleConditionType.Ep1ReadingCompare }>,
  context: RuleSummaryContext,
): string {
  const label = resolveDeviceLabel(
    condition.device.family_id,
    condition.device.device_id,
    context,
    condition.device.display_name,
  );
  const metricLabel =
    condition.metric === Ep1ReadingMetric.TemperatureC
      ? "temperature"
      : condition.metric === Ep1ReadingMetric.HumidityPct
        ? "humidity"
        : "illuminance";
  const unit =
    condition.metric === Ep1ReadingMetric.TemperatureC
      ? "°C"
      : condition.metric === Ep1ReadingMetric.HumidityPct
        ? "%"
        : "lx";
  return `${label} ${metricLabel} ${condition.comparison} ${String(condition.threshold)}${unit}`;
}

export function formatDeviceActionPhrase(
  action: RuleActionType,
  deviceLabel: string,
): string {
  switch (action) {
    case RuleActionType.TurnOn:
      return `Turn on ${deviceLabel}`;
    case RuleActionType.TurnOff:
      return `Turn off ${deviceLabel}`;
    case RuleActionType.Open:
      return `Open ${deviceLabel}`;
    case RuleActionType.Close:
      return `Close ${deviceLabel}`;
    case RuleActionType.Pause:
      return `Pause ${deviceLabel}`;
    case RuleActionType.Resume:
      return `Resume ${deviceLabel}`;
  }
}

function walkRuleConditions(
  conditions: readonly RuleConditionOut[],
  visit: (condition: RuleConditionOut) => void,
): void {
  for (const condition of conditions) {
    visit(condition);
    if (condition.type === RuleConditionType.All || condition.type === RuleConditionType.Any) {
      walkRuleConditions(condition.conditions, visit);
    }
  }
}

export function summarizeRule(
  rule: RuleOut,
  context: RuleSummaryContext,
): RuleSummarySections {
  const presence: string[] = [];
  const timing: string[] = [];
  const devices: string[] = [];
  walkRuleConditions(rule.conditions.all, (condition) => {
    if (condition.type === RuleConditionType.UsersInsideGeofenceForS) {
      presence.push(formatGeofenceDwellLabel(condition, context));
      return;
    }
    if (condition.type === RuleConditionType.UsersOutsideGeofenceForS) {
      presence.push(formatGeofenceAwayDwellLabel(condition, context));
      return;
    }
    if (condition.type === RuleConditionType.UsersMinDistanceFromHomeM) {
      presence.push(formatMinDistanceFromHomeLabel(condition, context));
      return;
    }
    if (
      condition.type === RuleConditionType.UsersInsideGeofence
      || condition.type === RuleConditionType.UsersOutsideGeofence
    ) {
      presence.push(formatPresenceEventLabel(condition, context));
      return;
    }
    if (
      condition.type === RuleConditionType.DevicesAllInState
      || condition.type === RuleConditionType.DevicesAnyInState
      || condition.type === RuleConditionType.DevicesAnyInStateForS
    ) {
      devices.push(formatDeviceStateCondition(condition, context));
      return;
    }
    if (condition.type === RuleConditionType.Ep1ReadingCompare) {
      devices.push(formatEp1ReadingCompareCondition(condition, context));
      return;
    }
    const timingLine = formatTimingCondition(condition);
    if (timingLine !== null) {
      timing.push(timingLine);
    }
  });
  const actions = rule.device_actions.map((entry) => {
    const label = resolveDeviceLabel(
      entry.family_id,
      entry.device_id,
      context,
      entry.display_name,
    );
    const phrase = formatDeviceActionPhrase(entry.action, label);
    const delay = entry.delay_s;
    if (delay !== undefined && delay !== null && delay > 0) {
      return `${phrase} (after ${delay}s)`;
    }
    return phrase;
  });
  return { presence, timing, devices, actions };
}

function appendSummarySection(
  parent: HTMLElement,
  heading: string,
  lines: readonly string[],
): void {
  if (lines.length === 0) {
    return;
  }
  const section = document.createElement("section");
  section.className = "rules-rule-summary-section";
  const title = document.createElement("h4");
  title.className = "rules-rule-summary-heading";
  title.textContent = heading;
  const list = document.createElement("ul");
  list.className = "rules-rule-summary-list";
  for (const line of lines) {
    const item = document.createElement("li");
    item.textContent = line;
    list.append(item);
  }
  section.append(title, list);
  parent.append(section);
}

function appendPresenceSummarySection(
  parent: HTMLElement,
  lines: readonly string[],
): void {
  if (lines.length === 0) {
    return;
  }
  if (lines.length === 1) {
    const section = document.createElement("section");
    section.className = "rules-rule-summary-section";
    const title = document.createElement("h4");
    title.className = "rules-rule-summary-heading rules-rule-summary-heading-event";
    title.textContent = lines[0] ?? "";
    section.append(title);
    parent.append(section);
    return;
  }
  appendSummarySection(parent, "Presence", lines);
}

/** Mount human-readable condition and action sections on a rule card. */
export function appendRuleSummaryBody(
  parent: HTMLElement,
  sections: RuleSummarySections,
): void {
  const body = document.createElement("div");
  body.className = "rules-rule-summary";
  appendPresenceSummarySection(body, sections.presence);
  appendSummarySection(body, "When", sections.timing);
  appendSummarySection(body, "If", sections.devices);
  appendSummarySection(body, "Then", sections.actions);
  if (body.childElementCount > 0) {
    parent.append(body);
  }
}
