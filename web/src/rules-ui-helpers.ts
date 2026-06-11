// Shared Automations hub UI helpers (info badges, family labels, toggles).

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

export function firstNameFromDisplayName(displayName: string): string {
  const trimmed = displayName.trim();
  if (trimmed === "") {
    return trimmed;
  }
  return trimmed.split(/\s+/)[0] ?? trimmed;
}

export function titleCaseParticipantId(participantId: string): string {
  return participantId
    .split(/[-_]/)
    .filter((part) => part.length > 0)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase())
    .join(" ");
}

export function participantLabelFromId(
  participantId: string,
  displayName?: string | null,
): string {
  const trimmed = displayName?.trim() ?? "";
  if (trimmed !== "" && trimmed.toLowerCase() !== participantId.toLowerCase()) {
    return firstNameFromDisplayName(trimmed);
  }
  return titleCaseParticipantId(participantId);
}

export function resolveParticipantDisplayName(
  participantId: string,
  displayName: string,
): string {
  const trimmed = displayName.trim();
  if (trimmed === "" || trimmed.toLowerCase() === participantId.toLowerCase()) {
    return titleCaseParticipantId(participantId);
  }
  return trimmed;
}

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
