// Human-readable Automations rule summaries for the Rules tab.

import {
  ALL_DAYS_OF_WEEK,
  DAY_OF_WEEK_LABELS,
  WEEKDAY_DAYS,
  WEEKEND_DAYS,
} from "./rules-ui-helpers.js";
import type {
  GeofenceOut,
  RuleActionDeviceOut,
  RuleActionType,
  RuleConditionOut,
  RuleOut,
  UserOut,
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

export function resolveDeviceLabel(
  familyId: string,
  deviceId: string,
  context: RuleSummaryContext,
): string {
  const key = deviceKey(familyId, deviceId);
  const direct = context.deviceLabelByKey.get(key);
  if (direct !== undefined && direct !== "" && !looksLikeIpv4(direct)) {
    return direct;
  }
  if (direct !== undefined && direct !== "") {
    return direct;
  }
  return deviceId;
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
      condition.type === "users_inside_geofence"
      || condition.type === "users_outside_geofence"
    ) {
      for (const userId of condition.user_ids) {
        ids.add(userId);
      }
      return;
    }
    if (condition.type === "all" || condition.type === "any") {
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

export function formatPresenceEventLabel(
  condition: Extract<
    RuleConditionOut,
    { type: "users_inside_geofence" | "users_outside_geofence" }
  >,
  context: RuleSummaryContext,
): string {
  const names = userDisplayNames(condition.user_ids, context);
  const where = geofenceLabel(condition.geofence_id, context);
  const who = joinNames(names);
  if (condition.type === "users_inside_geofence") {
    return `When ${who} enter ${where}`;
  }
  return `When ${who} leave ${where}`;
}

export function formatTimingCondition(condition: RuleConditionOut): string | null {
  switch (condition.type) {
    case "users_inside_geofence":
    case "users_outside_geofence":
      return null;
    case "all":
    case "any":
      return null;
    case "after_sunset": {
      const offset = condition.offset_minutes;
      const start =
        offset > 0
          ? `At least ${offset} minute${offset === 1 ? "" : "s"} after sunset`
          : "After sunset";
      return `${start} until midnight`;
    }
    case "before_sunrise": {
      const offset = condition.offset_minutes;
      const end =
        offset > 0
          ? `until ${offset} minute${offset === 1 ? "" : "s"} before sunrise`
          : "until sunrise";
      return `After midnight ${end}`;
    }
    case "after_local_time":
      return `After ${formatLocalTime(condition.time_hhmm)}`;
    case "before_local_time":
      return `Before ${formatLocalTime(condition.time_hhmm)}`;
    case "local_time_window":
      return `Between ${formatLocalTime(condition.start_hhmm)} and ${formatLocalTime(condition.end_hhmm)}`;
    case "days_of_week":
      return formatDaysOfWeek(condition.days);
  }
}

export function formatDeviceActionPhrase(
  action: RuleActionType,
  deviceLabel: string,
): string {
  switch (action) {
    case "turn_on":
      return `Turn on ${deviceLabel}`;
    case "turn_off":
      return `Turn off ${deviceLabel}`;
    case "open":
      return `Open ${deviceLabel}`;
    case "close":
      return `Close ${deviceLabel}`;
    case "pause":
      return `Pause ${deviceLabel}`;
    case "resume":
      return `Resume ${deviceLabel}`;
  }
}

function walkRuleConditions(
  conditions: readonly RuleConditionOut[],
  visit: (condition: RuleConditionOut) => void,
): void {
  for (const condition of conditions) {
    visit(condition);
    if (condition.type === "all" || condition.type === "any") {
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
  walkRuleConditions(rule.conditions.all, (condition) => {
    if (
      condition.type === "users_inside_geofence"
      || condition.type === "users_outside_geofence"
    ) {
      presence.push(formatPresenceEventLabel(condition, context));
      return;
    }
    const timingLine = formatTimingCondition(condition);
    if (timingLine !== null) {
      timing.push(timingLine);
    }
  });
  const actions = rule.device_actions.map((entry) => {
    const label = resolveDeviceLabel(entry.family_id, entry.device_id, context);
    return formatDeviceActionPhrase(entry.action, label);
  });
  return { presence, timing, actions };
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
  appendSummarySection(body, "Then", sections.actions);
  if (body.childElementCount > 0) {
    parent.append(body);
  }
}
