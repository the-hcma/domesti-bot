// Read-only automation rule wiring panel (file-backed bundle).

import {
  appendRuleSummaryBody,
  buildRuleSummaryContext,
  formatDeviceActionPhrase,
  formatDeviceStateCondition,
  formatGeofenceAwayDwellLabel,
  formatGeofenceDwellLabel,
  formatPresenceEventLabel,
  formatTimingCondition,
  resolveDeviceLabel,
  summarizeRule,
  type RuleSummaryContext,
} from "./rule-summary.js";
import {
  appendRuleLastMetLine,
  createBrokenRuleBadge,
  ruleStatusHeadline,
} from "./rules-ui-helpers.js";
import type {
  GeofenceOut,
  UserOut,
  RuleActionDeviceOut,
  RuleConditionOut,
  RuleConditionStatusOut,
  RuleOut,
  RuleStatusSummaryOut,
} from "./types.js";

export interface RuleInspectorMountOptions {
  liveStatus?: RuleStatusSummaryOut;
  onClose: () => void;
}

function appendConditionTree(
  parent: HTMLElement,
  conditions: readonly RuleConditionOut[],
  context: RuleSummaryContext,
): void {
  const list = document.createElement("ul");
  list.className = "rules-condition-tree";
  for (const condition of conditions) {
    const item = document.createElement("li");
    if (condition.type === "all" || condition.type === "any") {
      item.textContent = condition.type === "all" ? "All of" : "Any of";
      appendConditionTree(item, condition.conditions, context);
    } else if (condition.type === "users_inside_geofence_for_s") {
      item.textContent = formatGeofenceDwellLabel(condition, context);
    } else if (condition.type === "users_outside_geofence_for_s") {
      item.textContent = formatGeofenceAwayDwellLabel(condition, context);
    } else if (
      condition.type === "users_inside_geofence"
      || condition.type === "users_outside_geofence"
    ) {
      item.textContent = formatPresenceEventLabel(condition, context);
    } else if (
      condition.type === "devices_all_on"
      || condition.type === "devices_any_in_state_for_s"
      || condition.type === "devices_any_off"
      || condition.type === "devices_any_on"
      || condition.type === "devices_any_open"
    ) {
      item.textContent = formatDeviceStateCondition(condition, context);
    } else {
      const timing = formatTimingCondition(condition);
      item.textContent = timing ?? condition.type;
    }
    list.append(item);
  }
  parent.append(list);
}

function isEdgePresenceStatusRow(
  triggers: RuleStatusSummaryOut["triggers"],
  cond: RuleConditionStatusOut,
): boolean {
  if (!triggers.includes("edge_true") || triggers.includes("scheduled")) {
    return false;
  }
  if (
    cond.condition.type === "users_inside_geofence"
    || cond.condition.type === "users_outside_geofence"
  ) {
    return true;
  }
  if (cond.condition.type === "any") {
    return cond.condition.conditions.every(
      (child) => child.type === "users_inside_geofence"
        || child.type === "users_outside_geofence",
    );
  }
  return false;
}

function appendDefinitionRow(dl: HTMLDListElement, term: string, value: string): void {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = value;
  dl.append(dt, dd);
}

function appendLiveStatusSection(
  parent: HTMLElement,
  liveStatus: RuleStatusSummaryOut,
): void {
  const section = document.createElement("section");
  section.className = "rules-inspector-section";
  const title = document.createElement("h4");
  title.className = "rules-rule-summary-heading";
  title.textContent = "Live status";
  section.append(title);

  const headline = document.createElement("p");
  headline.className = "rules-card-meta";
  headline.textContent = ruleStatusHeadline(liveStatus);
  section.append(headline);

  appendRuleLastMetLine(section, liveStatus);

  if (liveStatus.last_error !== null) {
    const error = document.createElement("p");
    error.className = "rules-card-warn";
    error.textContent = liveStatus.last_error;
    section.append(error);
  }

  const condList = document.createElement("ul");
  condList.className = "rules-condition-list";
  for (const cond of liveStatus.conditions) {
    const li = document.createElement("li");
    const presenceOnly = isEdgePresenceStatusRow(liveStatus.triggers, cond);
    if (presenceOnly) {
      li.className = "rules-condition-presence";
      li.textContent = `${cond.label} — ${cond.detail}`;
    } else {
      li.className = cond.met ? "rules-condition-met" : "rules-condition-unmet";
      li.textContent = `${cond.met ? "✓" : "✗"} ${cond.label} — ${cond.detail}`;
    }
    condList.append(li);
  }
  section.append(condList);
  parent.append(section);
}

export function buildInspectorContext(
  users: readonly UserOut[],
  geofences: readonly GeofenceOut[],
  actionDevices: readonly RuleActionDeviceOut[],
): RuleSummaryContext {
  return buildRuleSummaryContext(users, geofences, actionDevices);
}

export function mountRuleInspectorPanel(
  parent: HTMLElement,
  rule: RuleOut,
  context: RuleSummaryContext,
  options: RuleInspectorMountOptions,
): void {
  const header = document.createElement("div");
  header.className = "rules-rule-editor-header";
  const title = document.createElement("h3");
  title.className = "rules-rule-editor-title";
  title.textContent = rule.label;
  header.append(title);
  if (options.liveStatus !== undefined && options.liveStatus.reference_issues.length > 0) {
    header.append(createBrokenRuleBadge(options.liveStatus.reference_issues));
  }

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Read-only view of automation-rules.json. Edit the file on the server and restart to change this rule.";

  const meta = document.createElement("dl");
  meta.className = "rules-inspector-meta";
  appendDefinitionRow(meta, "Rule id", rule.id);
  appendDefinitionRow(meta, "Enabled", rule.enabled ? "Yes" : "No");
  appendDefinitionRow(meta, "Triggers", rule.triggers.join(", "));
  if (rule.triggers.includes("scheduled")) {
    appendDefinitionRow(meta, "Schedule (cron)", rule.schedule_cron ?? "(none)");
  }
  if (rule.fire_once_per_local_day === true) {
    appendDefinitionRow(meta, "Fire once per local day", "Yes");
  }
  appendDefinitionRow(meta, "Cooldown", `${rule.cooldown_s} s`);
  appendDefinitionRow(
    meta,
    "Min location accuracy",
    `${rule.min_location_accuracy_m} m`,
  );
  if (rule.accuracy_edge_grace_s != null && rule.accuracy_edge_grace_s > 0) {
    appendDefinitionRow(
      meta,
      "Accuracy edge grace",
      `${rule.accuracy_edge_grace_s} s`,
    );
  }
  if (rule.notify_on_fire && rule.notification_emails.length > 0) {
    appendDefinitionRow(meta, "Email on fire", rule.notification_emails.join(", "));
  }

  const summaryHost = document.createElement("div");
  appendRuleSummaryBody(summaryHost, summarizeRule(rule, context));

  const conditionsSection = document.createElement("section");
  conditionsSection.className = "rules-inspector-section";
  const conditionsTitle = document.createElement("h4");
  conditionsTitle.className = "rules-rule-summary-heading";
  conditionsTitle.textContent = "Conditions";
  conditionsSection.append(conditionsTitle);
  appendConditionTree(conditionsSection, rule.conditions.all, context);

  const actionsSection = document.createElement("section");
  actionsSection.className = "rules-inspector-section";
  const actionsTitle = document.createElement("h4");
  actionsTitle.className = "rules-rule-summary-heading";
  actionsTitle.textContent = "Device actions";
  actionsSection.append(actionsTitle);
  if (rule.device_actions.length === 0) {
    const empty = document.createElement("p");
    empty.className = "rules-card-meta";
    empty.textContent = "None";
    actionsSection.append(empty);
  } else {
    const actionList = document.createElement("ul");
    actionList.className = "rules-rule-summary-list";
    for (const action of rule.device_actions) {
      const item = document.createElement("li");
      const label = resolveDeviceLabel(action.family_id, action.device_id, context);
      item.textContent = `${action.family_id} · ${formatDeviceActionPhrase(action.action, label)}`;
      actionList.append(item);
    }
    actionsSection.append(actionList);
  }

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn";
  closeBtn.textContent = "Back to rules";
  closeBtn.addEventListener("click", options.onClose);
  actions.append(closeBtn);

  parent.append(header, lead, meta, summaryHost, conditionsSection, actionsSection);
  if (options.liveStatus !== undefined) {
    appendLiveStatusSection(parent, options.liveStatus);
  }
  parent.append(actions);
}
