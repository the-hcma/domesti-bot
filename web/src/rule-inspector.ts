// Read-only automation rule wiring panel (file-backed bundle).

import { createAuditedTimeElement } from "./format-timestamp.js";
import {
  appendRuleSummaryBody,
  buildRuleSummaryContext,
  formatDeviceActionPhrase,
  formatPresenceEventLabel,
  formatTimingCondition,
  resolveDeviceLabel,
  summarizeRule,
  type RuleSummaryContext,
} from "./rule-summary.js";
import type {
  GeofenceOut,
  ParticipantOut,
  RuleActionDeviceOut,
  RuleConditionOut,
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
    } else if (
      condition.type === "participants_inside_geofence"
      || condition.type === "participants_outside_geofence"
    ) {
      item.textContent = formatPresenceEventLabel(condition, context);
    } else {
      const timing = formatTimingCondition(condition);
      item.textContent = timing ?? condition.type;
    }
    list.append(item);
  }
  parent.append(list);
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
  headline.textContent = liveStatus.condition_currently_true
    ? "All conditions are currently met."
    : "Waiting — not all conditions are met yet.";
  section.append(headline);

  if (liveStatus.last_fired_at !== null) {
    const fired = document.createElement("p");
    fired.className = "rules-card-meta";
    fired.append(document.createTextNode("Last fired "));
    fired.append(createAuditedTimeElement(liveStatus.last_fired_at));
    section.append(fired);
  }

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
    li.className = cond.met ? "rules-condition-met" : "rules-condition-unmet";
    li.textContent = `${cond.met ? "✓" : "✗"} ${cond.label} — ${cond.detail}`;
    condList.append(li);
  }
  section.append(condList);
  parent.append(section);
}

export function buildInspectorContext(
  participants: readonly ParticipantOut[],
  geofences: readonly GeofenceOut[],
  actionDevices: readonly RuleActionDeviceOut[],
): RuleSummaryContext {
  return buildRuleSummaryContext(participants, geofences, actionDevices);
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

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Read-only view of automation-rules.json. Edit the file on the server and restart to change this rule.";

  const meta = document.createElement("dl");
  meta.className = "rules-inspector-meta";
  appendDefinitionRow(meta, "Rule id", rule.id);
  appendDefinitionRow(meta, "Enabled", rule.enabled ? "Yes" : "No");
  appendDefinitionRow(meta, "Trigger", rule.trigger);
  appendDefinitionRow(meta, "Cooldown", `${rule.cooldown_s} s`);
  appendDefinitionRow(
    meta,
    "Min fix accuracy",
    `${rule.min_fix_accuracy_m} m`,
  );
  if (rule.notify_on_fire && rule.notification_email !== null) {
    appendDefinitionRow(meta, "Email on fire", rule.notification_email);
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
