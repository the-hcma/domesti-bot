// Shared Automations hub UI helpers (info badges, family labels, toggles).

import { createAuditedTimeElement } from "./format-timestamp.js";
import type { RuleTrigger } from "./types.js";

export const ALL_DAYS_OF_WEEK = [0, 1, 2, 3, 4, 5, 6] as const;

export const DAY_OF_WEEK_LABELS = [
  "Sun",
  "Mon",
  "Tue",
  "Wed",
  "Thu",
  "Fri",
  "Sat",
] as const;

export const FAMILY_ACTION_GROUP_LABELS: Record<string, string> = {
  kasa: "Lights & plugs",
  sonos: "Sonos zones",
  tailwind: "Garage doors",
};

export const WEEKDAY_DAYS = [1, 2, 3, 4, 5] as const;

export const WEEKEND_DAYS = [0, 6] as const;

export interface DayOfWeekPicker {
  fieldset: HTMLFieldSetElement;
  getSelectedDays: () => number[];
  setSelectedDays: (days: readonly number[]) => void;
}

export function createDayOfWeekPicker(
  initialDays: readonly number[],
): DayOfWeekPicker {
  const selected = new Set(initialDays);
  const fieldset = document.createElement("fieldset");
  fieldset.className = "rules-editor-fieldset rules-editor-subfieldset";
  const legend = document.createElement("legend");
  legend.textContent = "Days of week";
  fieldset.append(legend);

  const shortcutRow = document.createElement("div");
  shortcutRow.className = "rules-day-shortcuts";
  const dayRows = document.createElement("div");
  dayRows.className = "rules-day-grid";
  const dayInputs: HTMLInputElement[] = [];

  const syncDayInputs = (): void => {
    for (const input of dayInputs) {
      input.checked = selected.has(Number(input.value));
    }
  };

  const applyDays = (days: readonly number[]): void => {
    selected.clear();
    for (const day of days) {
      selected.add(day);
    }
    syncDayInputs();
  };

  for (const [label, days] of [
    ["Weekdays", WEEKDAY_DAYS],
    ["Weekend", WEEKEND_DAYS],
    ["All days", ALL_DAYS_OF_WEEK],
  ] as const) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn btn-secondary rules-day-shortcut";
    btn.textContent = label;
    btn.addEventListener("click", () => {
      applyDays(days);
    });
    shortcutRow.append(btn);
  }
  fieldset.append(shortcutRow);

  for (let day = 0; day < 7; day += 1) {
    const row = document.createElement("label");
    row.className = "rules-check-row rules-day-toggle";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.value = String(day);
    cb.checked = selected.has(day);
    cb.addEventListener("change", () => {
      if (cb.checked) {
        selected.add(day);
      } else {
        selected.delete(day);
      }
    });
    dayInputs.push(cb);
    row.append(cb, document.createTextNode(` ${DAY_OF_WEEK_LABELS[day] ?? String(day)}`));
    dayRows.append(row);
  }
  fieldset.append(dayRows);

  return {
    fieldset,
    getSelectedDays: () =>
      [...selected].sort((a, b) => a - b),
    setSelectedDays: applyDays,
  };
}

let infoPopoverDocumentListenerInstalled = false;
let closeOpenInfoPopover: (() => void) | null = null;

function ensureInfoPopoverDocumentListener(): void {
  if (infoPopoverDocumentListenerInstalled) {
    return;
  }
  infoPopoverDocumentListenerInstalled = true;
  document.addEventListener("click", () => {
    if (closeOpenInfoPopover !== null) {
      closeOpenInfoPopover();
      closeOpenInfoPopover = null;
    }
  });
}

export function createInfoBadge(
  label: string,
  detail: string,
  example: string,
): HTMLSpanElement {
  ensureInfoPopoverDocumentListener();
  const wrap = document.createElement("span");
  wrap.className = "rules-info-badge-wrap";
  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = "rules-info-badge";
  badge.setAttribute("aria-label", `About ${label}`);
  badge.setAttribute("aria-expanded", "false");
  badge.textContent = "i";
  const popover = document.createElement("span");
  popover.className = "rules-info-popover";
  popover.hidden = true;
  popover.setAttribute("role", "tooltip");
  const title = document.createElement("strong");
  title.textContent = label;
  const body = document.createElement("span");
  body.className = "rules-info-popover-body";
  body.textContent = detail;
  const ex = document.createElement("span");
  ex.className = "rules-info-popover-example";
  ex.textContent = `Example: ${example}`;
  popover.append(title, body, ex);
  badge.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const willOpen = popover.hidden;
    if (closeOpenInfoPopover !== null) {
      closeOpenInfoPopover();
      closeOpenInfoPopover = null;
    }
    if (willOpen) {
      popover.hidden = false;
      badge.setAttribute("aria-expanded", "true");
      closeOpenInfoPopover = () => {
        popover.hidden = true;
        badge.setAttribute("aria-expanded", "false");
      };
    } else {
      badge.setAttribute("aria-expanded", "false");
    }
  });
  wrap.append(badge, popover);
  return wrap;
}

export function createFieldLabel(
  text: string,
  info?: { detail: string; example: string },
): HTMLSpanElement {
  const row = document.createElement("span");
  row.className = "rules-field-label-row";
  const label = document.createElement("span");
  label.textContent = text;
  row.append(label);
  if (info !== undefined) {
    row.append(createInfoBadge(text, info.detail, info.example));
  }
  return row;
}

export function createEnableToggle(
  initial: boolean,
  onChange: (next: boolean) => void,
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rules-enable-toggle";
  const sync = (on: boolean): void => {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.textContent = on ? "Enabled" : "Disabled";
    btn.classList.toggle("rules-enable-toggle-on", on);
  };
  sync(initial);
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const next = btn.getAttribute("aria-pressed") !== "true";
    sync(next);
    onChange(next);
  });
  return btn;
}

/** Reduce Chrome / password-manager autofill on non-login admin fields. */
/** UI label for a user: roster ``display_name`` when set, else ``user_id``. */
export function userDisplayLabel(userId: string, displayName?: string): string {
  const trimmed = (displayName ?? "").trim();
  return trimmed !== "" ? trimmed : userId;
}

let brokenRulePopoverIdSeq = 0;

function isNonCanonicalOnly(
  issues: readonly { kind: string; detail: string }[],
): boolean {
  return (
    issues.length > 0 &&
    issues.every((issue) => issue.kind === "non_canonical_device_id")
  );
}

export function createBrokenRuleBadge(
  issues: readonly { kind: string; detail: string }[],
): HTMLSpanElement {
  const warningOnly = isNonCanonicalOnly(issues);
  const wrap = document.createElement("span");
  wrap.className = "rules-broken-badge-wrap";

  const badge = document.createElement("span");
  badge.className = warningOnly
    ? "rules-broken-badge rules-warning-badge"
    : "rules-broken-badge";
  badge.tabIndex = 0;
  const label = document.createElement("span");
  label.className = "rules-broken-badge-label";
  label.textContent = warningOnly ? "Warning" : "Broken";
  badge.append(label);

  const hint = document.createElement("span");
  hint.className = "rules-broken-hover-hint";
  hint.textContent = "Hover for details";
  hint.setAttribute("aria-hidden", "true");

  const popoverId = `rules-broken-popover-${++brokenRulePopoverIdSeq}`;
  const popover = document.createElement("span");
  popover.className = "rules-broken-popover";
  popover.id = popoverId;
  popover.hidden = true;
  popover.setAttribute("role", "tooltip");
  const list = document.createElement("ul");
  list.className = "rules-broken-popover-list";
  for (const issue of issues) {
    const item = document.createElement("li");
    item.textContent = issue.detail;
    list.append(item);
  }
  popover.append(list);

  const summary = issues.map((issue) => issue.detail).join("; ");
  const kindLabel = warningOnly ? "Rule warning" : "Broken rule";
  badge.setAttribute(
    "aria-label",
    `${kindLabel}: ${summary}. Hover for details.`,
  );

  const showPopover = (): void => {
    popover.hidden = false;
    badge.setAttribute("aria-describedby", popoverId);
  };
  const hidePopover = (): void => {
    popover.hidden = true;
    badge.removeAttribute("aria-describedby");
  };

  wrap.addEventListener("mouseenter", showPopover);
  wrap.addEventListener("mouseleave", hidePopover);
  wrap.addEventListener("focusin", showPopover);
  wrap.addEventListener("focusout", (ev) => {
    const next = ev.relatedTarget;
    if (next instanceof Node && wrap.contains(next)) {
      return;
    }
    hidePopover();
  });

  wrap.append(badge, hint, popover);
  return wrap;
}

export function resolveRosterUser<T extends { user_id: string }>(
  reference: string,
  users: readonly T[],
): T | undefined {
  const trimmed = reference.trim();
  if (trimmed === "") {
    return undefined;
  }
  const exact = users.find((row) => row.user_id === trimmed);
  if (exact !== undefined) {
    return exact;
  }
  const lower = trimmed.toLowerCase();
  return users.find((row) => row.user_id.toLowerCase() === lower);
}

export function ruleStatusHeadline(rule: {
  condition_currently_true: boolean;
  last_fired_at: string | null;
  triggers: RuleTrigger[];
}): string {
  const hasDeviceState = rule.triggers.includes("device_state");
  const hasDwellSatisfied = rule.triggers.includes("dwell_satisfied");
  const hasEdge = rule.triggers.includes("edge_true");
  const hasScheduled = rule.triggers.includes("scheduled");
  if (hasEdge && !hasScheduled && !hasDeviceState && !hasDwellSatisfied) {
    if (rule.condition_currently_true) {
      return "Armed — fires on enter/leave";
    }
    return "Waiting — outside active window";
  }
  if (hasDeviceState && !hasScheduled && !hasDwellSatisfied) {
    return rule.condition_currently_true
      ? "Ready — conditions currently met"
      : "Armed — fires on device state change";
  }
  if (hasDwellSatisfied && !hasScheduled) {
    return rule.condition_currently_true
      ? "Ready — conditions currently met"
      : "Armed — fires when dwell threshold is satisfied";
  }
  if (hasScheduled) {
    return rule.condition_currently_true
      ? "Ready — conditions currently met"
      : "Waiting — conditions not met yet";
  }
  return rule.condition_currently_true
    ? "Ready — all conditions met"
    : "Waiting — conditions not met yet";
}

export function ruleLastMetLabel(triggers: RuleTrigger[]): string {
  if (triggers.includes("edge_true") && !triggers.includes("scheduled")) {
    return "Last met ";
  }
  return "Last fired ";
}

export function appendRuleLastMetLine(
  parent: HTMLElement,
  rule: {
    last_fired_at: string | null;
    next_evaluate_at: string | null;
    scheduled_detail: string | null;
    triggers: RuleTrigger[];
  },
): void {
  const fired = document.createElement("p");
  fired.className = "rules-card-meta";
  fired.append(document.createTextNode(ruleLastMetLabel(rule.triggers)));
  if (rule.last_fired_at !== null) {
    fired.append(createAuditedTimeElement(rule.last_fired_at));
  } else {
    fired.append("Never");
  }
  parent.append(fired);
  if (rule.triggers.includes("scheduled") && rule.next_evaluate_at != null) {
    const next = document.createElement("p");
    next.className = "rules-card-meta";
    next.append(document.createTextNode("Next evaluate "));
    next.append(createAuditedTimeElement(rule.next_evaluate_at));
    parent.append(next);
  }
  if (rule.scheduled_detail != null && rule.scheduled_detail !== "") {
    const detail = document.createElement("p");
    detail.className = "rules-card-meta";
    detail.textContent = rule.scheduled_detail;
    parent.append(detail);
  }
}

export function preventBrowserAutofill(input: HTMLInputElement): void {
  input.setAttribute("autocomplete", "off");
  input.setAttribute("data-1p-ignore", "true");
  input.setAttribute("data-bwignore", "true");
  input.setAttribute("data-form-type", "other");
  input.setAttribute("data-lpignore", "true");
  input.setAttribute("readonly", "readonly");
  input.addEventListener("focus", () => {
    input.removeAttribute("readonly");
  });
}
