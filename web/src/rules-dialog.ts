// Desktop Automations hub — Status, Conditions, automations, geofences, mail (mock-backed).

import type { RulesDataSource } from "./rules-data-source.js";
import { createRulesDataSource } from "./rules-data-source.js";
import { DEFAULT_MIN_FIX_ACCURACY_M } from "./rules-evaluate.js";
import { mountPresenceMiniMap } from "./presence-mini-map.js";
import {
  ALL_DAYS_OF_WEEK,
  createDayOfWeekPicker,
  createEnableToggle,
  createFieldLabel,
  FAMILY_ACTION_GROUP_LABELS,
} from "./rules-ui-helpers.js";
import type {
  GeofenceOut,
  ParticipantFixOut,
  ParticipantStatusOut,
  RuleActionDeviceOut,
  RuleActionType,
  RuleConditionOut,
  RuleDeviceActionOut,
  RuleOut,
  RulesStatusOut,
  RulesSunOut,
  TimeConditionTemplateOut,
  UIDeviceKind,
} from "./types.js";

type RulesTabId =
  | "conditions"
  | "geofences"
  | "mail"
  | "participants"
  | "rules"
  | "status";

function actionOptionsForKind(kind: UIDeviceKind): RuleActionType[] {
  switch (kind) {
    case "switch":
      return ["turn_on", "turn_off"];
    case "door":
      return ["open", "close"];
    case "speaker":
      return ["pause", "resume"];
  }
}

function appendLabeledField(
  parent: HTMLElement,
  labelEl: HTMLElement,
  control: HTMLElement,
): void {
  const field = document.createElement("label");
  field.className = "settings-dialog-field";
  field.append(labelEl, control);
  parent.append(field);
}

function createDialogCloseButton(dialog: HTMLDialogElement): HTMLButtonElement {
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "settings-dialog-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "\u00d7";
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  return closeBtn;
}

function formatActionLabel(action: RuleActionType): string {
  return action.replaceAll("_", " ");
}

function formatAge(seconds: number | null): string {
  if (seconds === null) {
    return "never";
  }
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} min ago`;
  }
  return `${Math.floor(seconds / 3600)} h ago`;
}

function formatLocalTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  } catch {
    return iso;
  }
}

function hhmmToTimeInput(hhmm: string): string {
  if (/^\d{1,2}:\d{2}$/.test(hhmm)) {
    const [h, m] = hhmm.split(":");
    return `${h?.padStart(2, "0") ?? "00"}:${m ?? "00"}`;
  }
  return "";
}

function slugifyId(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function sunriseStatusMessage(sun: RulesSunOut): { dynamicLabel: string; primary: string } {
  if (!sun.is_dark) {
    return {
      dynamicLabel: "Before sunrise (dynamic)",
      primary: `Daytime — next sunrise at ${formatLocalTime(sun.sunrise_at)}`,
    };
  }
  return {
    dynamicLabel: "Sunrise (dynamic)",
    primary: `Dark until sunrise at ${formatLocalTime(sun.sunrise_at)}`,
  };
}

function sunsetStatusMessage(sun: RulesSunOut): { dynamicLabel: string; primary: string } {
  if (sun.is_dark) {
    return {
      dynamicLabel: "After sunset (dynamic)",
      primary: `Dark now — sunset was ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  return {
    dynamicLabel: "Sunset (dynamic)",
    primary: `Light until sunset at ${formatLocalTime(sun.sunset_at)}`,
  };
}

function sunStatusMessage(sun: RulesSunOut): { dynamicLabel: string; primary: string } {
  return sunsetStatusMessage(sun);
}

function templateToCondition(
  template: TimeConditionTemplateOut,
): RuleConditionOut {
  if (template.type === "after_local_time") {
    return { type: "after_local_time", time_hhmm: template.time_hhmm };
  }
  return { type: "before_local_time", time_hhmm: template.time_hhmm };
}

class RulesHubController {
  private readonly body: HTMLDivElement;
  private readonly dialog: HTMLDialogElement;
  private readonly mockPill: HTMLSpanElement;
  private readonly panel: HTMLDivElement;
  private activeTab: RulesTabId = "status";
  private dataSource: RulesDataSource;
  private status: RulesStatusOut | null = null;

  constructor(dataSource: RulesDataSource) {
    this.dataSource = dataSource;
    this.dialog = document.createElement("dialog");
    this.dialog.className = "settings-dialog rules-dialog automations-dialog";
    this.panel = document.createElement("div");
    this.panel.className = "settings-dialog-panel";
    const header = document.createElement("header");
    header.className = "settings-dialog-header rules-dialog-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "rules-dialog-title-wrap";
    const title = document.createElement("h2");
    title.textContent = "Automations";
    this.mockPill = document.createElement("span");
    this.mockPill.className = "rules-mock-pill";
    this.mockPill.textContent = "Mock data";
    this.mockPill.hidden = !dataSource.isMock();
    titleWrap.append(title, this.mockPill);
    header.append(titleWrap, createDialogCloseButton(this.dialog));
    const tabBar = document.createElement("div");
    tabBar.className = "rules-tab-bar";
    tabBar.setAttribute("role", "tablist");
    for (const tab of [
      ["status", "Status"],
      ["conditions", "Conditions"],
      ["rules", "Rules"],
      ["geofences", "Geofences"],
      ["participants", "Participants"],
      ["mail", "Mail"],
    ] as const) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "rules-tab";
      btn.dataset.tab = tab[0];
      btn.setAttribute("role", "tab");
      btn.textContent = tab[1];
      btn.addEventListener("click", () => {
        void this.setTab(tab[0]);
      });
      tabBar.append(btn);
    }
    this.body = document.createElement("div");
    this.body.className = "settings-dialog-body rules-dialog-body";
    this.panel.append(header, tabBar, this.body);
    this.dialog.append(this.panel);
    this.dialog.addEventListener("close", () => {
      this.dialog.remove();
    });
    this.dialog.addEventListener("click", (ev) => {
      if (ev.target === this.dialog) {
        this.dialog.close();
      }
    });
  }

  async open(): Promise<void> {
    document.body.append(this.dialog);
    this.dialog.showModal();
    await this.refresh();
  }

  private appendDeviceActionGroups(
    actionsWrap: HTMLElement,
    actionDevices: RuleActionDeviceOut[],
    existing: RuleOut | null,
  ): {
    actionSelect: HTMLSelectElement;
    checkbox: HTMLInputElement;
    device: RuleActionDeviceOut;
  }[] {
    const deviceActionState = new Map<string, RuleDeviceActionOut>();
    for (const entry of existing?.device_actions ?? []) {
      deviceActionState.set(`${entry.family_id}\0${entry.device_id}`, entry);
    }
    const byFamily = new Map<string, RuleActionDeviceOut[]>();
    for (const device of actionDevices) {
      const list = byFamily.get(device.family_id) ?? [];
      list.push(device);
      byFamily.set(device.family_id, list);
    }
    const deviceRows: {
      actionSelect: HTMLSelectElement;
      checkbox: HTMLInputElement;
      device: RuleActionDeviceOut;
    }[] = [];
    for (const familyId of [...byFamily.keys()].sort()) {
      const group = document.createElement("div");
      group.className = "rules-device-action-group";
      const heading = document.createElement("h4");
      heading.className = "rules-device-action-group-title";
      heading.textContent = FAMILY_ACTION_GROUP_LABELS[familyId] ?? familyId;
      group.append(heading);
      for (const device of byFamily.get(familyId) ?? []) {
        const row = document.createElement("div");
        row.className = "rules-device-action-row";
        const key = `${device.family_id}\0${device.device_id}`;
        const existingAction = deviceActionState.get(key);
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = existingAction !== undefined;
        const label = document.createElement("label");
        label.className = "rules-device-action-label";
        const name = document.createElement("span");
        name.textContent = device.label;
        const actionSelect = document.createElement("select");
        for (const action of actionOptionsForKind(device.kind)) {
          const opt = document.createElement("option");
          opt.value = action;
          opt.textContent = formatActionLabel(action);
          actionSelect.append(opt);
        }
        const defaultAction = actionOptionsForKind(device.kind)[0] ?? "turn_on";
        actionSelect.value = existingAction?.action ?? defaultAction;
        actionSelect.disabled = !checkbox.checked;
        checkbox.addEventListener("change", () => {
          actionSelect.disabled = !checkbox.checked;
        });
        label.append(checkbox, name, actionSelect);
        row.append(label);
        group.append(row);
        deviceRows.push({ actionSelect, checkbox, device });
      }
      actionsWrap.append(group);
    }
    return deviceRows;
  }

  private async openRuleEditor(existing: RuleOut | null): Promise<void> {
    const geofences = await this.dataSource.listGeofences();
    const participants = await this.dataSource.listParticipants();
    const actionDevices = await this.dataSource.listActionDevices();
    const timeTemplates = await this.dataSource.listTimeConditionTemplates();

    const editor = document.createElement("div");
    editor.className = "rules-editor-panel";
    const form = document.createElement("form");
    form.className = "rules-editor-form";

    const labelInput = document.createElement("input");
    labelInput.value = existing?.label ?? "";
    labelInput.required = true;
    appendLabeledField(
      form,
      createFieldLabel("Name"),
      labelInput,
    );

    const idInput = document.createElement("input");
    idInput.value = existing?.id ?? "";
    idInput.required = true;
    idInput.readOnly = existing !== null;
    appendLabeledField(
      form,
      createFieldLabel("Rule id"),
      idInput,
    );

    const conditionsField = document.createElement("fieldset");
    conditionsField.className = "rules-editor-fieldset";
    const conditionsLegend = document.createElement("legend");
    conditionsLegend.textContent = "Conditions";
    conditionsField.append(conditionsLegend);

    const whoField = document.createElement("fieldset");
    whoField.className = "rules-editor-fieldset rules-editor-subfieldset";
    const whoLegend = document.createElement("legend");
    whoLegend.textContent = "Who must be home";
    whoField.append(whoLegend);
    const selectedParticipants = new Set<string>(
      existing?.conditions.all.find((c) => c.type === "participants_inside_geofence")
        ?.participant_ids ?? ["henrique", "kristen"],
    );
    for (const p of participants) {
      const row = document.createElement("label");
      row.className = "rules-check-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = p.participant_id;
      cb.checked = selectedParticipants.has(p.participant_id);
      row.append(
        cb,
        document.createTextNode(` ${p.display_name} (${p.tracking_device_label})`),
      );
      whoField.append(row);
    }
    conditionsField.append(whoField);

    const whereSelect = document.createElement("select");
    for (const g of geofences) {
      const opt = document.createElement("option");
      opt.value = g.geofence_id;
      opt.textContent = g.label;
      whereSelect.append(opt);
    }
    const existingGeofence =
      existing?.conditions.all.find((c) => c.type === "participants_inside_geofence")
        ?.geofence_id ?? "house";
    whereSelect.value = existingGeofence;
    appendLabeledField(
      conditionsField,
      createFieldLabel("Geofence"),
      whereSelect,
    );

    const existingDayCondition = existing?.conditions.all.find(
      (c): c is Extract<RuleConditionOut, { type: "days_of_week" }> =>
        c.type === "days_of_week",
    );
    const dayPicker = createDayOfWeekPicker(
      existingDayCondition?.days ?? ALL_DAYS_OF_WEEK,
    );
    conditionsField.append(dayPicker.fieldset);

    const timeField = document.createElement("fieldset");
    timeField.className = "rules-editor-fieldset rules-editor-subfieldset";
    const timeLegend = document.createElement("legend");
    timeLegend.textContent = "Time of day";
    timeField.append(timeLegend);

    const sunsetRow = document.createElement("label");
    sunsetRow.className = "rules-check-row";
    const sunsetCb = document.createElement("input");
    sunsetCb.type = "checkbox";
    sunsetCb.checked =
      existing?.conditions.all.some((c) => c.type === "after_sunset") ?? true;
    sunsetRow.append(sunsetCb, document.createTextNode(" After sunset (dynamic)"));
    timeField.append(sunsetRow);

    const sunriseRow = document.createElement("label");
    sunriseRow.className = "rules-check-row";
    const sunriseCb = document.createElement("input");
    sunriseCb.type = "checkbox";
    sunriseCb.checked =
      existing?.conditions.all.some((c) => c.type === "before_sunrise") ?? false;
    sunriseRow.append(sunriseCb, document.createTextNode(" Before sunrise (dynamic)"));
    timeField.append(sunriseRow);

    const afterTime = document.createElement("input");
    afterTime.type = "time";
    const existingAfter =
      existing?.conditions.all.find((c) => c.type === "after_local_time")?.time_hhmm ?? "";
    afterTime.value = hhmmToTimeInput(existingAfter);
    appendLabeledField(
      timeField,
      createFieldLabel(
        "After clock time (optional)",
        {
          detail:
            "Require local time to be at or past this hour on days the rule is evaluated.",
          example: "22:00 so porch lights wait until 10 PM even when you arrive earlier.",
        },
      ),
      afterTime,
    );

    const beforeTime = document.createElement("input");
    beforeTime.type = "time";
    const existingBefore =
      existing?.conditions.all.find((c) => c.type === "before_local_time")?.time_hhmm ?? "";
    beforeTime.value = hhmmToTimeInput(existingBefore);
    appendLabeledField(
      timeField,
      createFieldLabel(
        "Before clock time (optional)",
        {
          detail:
            "Require local time to be before this hour. Leave blank when not needed.",
          example: "06:00 for a quiet-hours rule that ends at sunrise routines.",
        },
      ),
      beforeTime,
    );

    if (timeTemplates.length > 0) {
      const templatesLegend = document.createElement("p");
      templatesLegend.className = "rules-field-hint";
      templatesLegend.textContent = "Reusable templates from the Conditions tab:";
      timeField.append(templatesLegend);
      const selectedTemplateIds = new Set(
        existing?.conditions.all
          .filter(
            (c) => c.type === "after_local_time" || c.type === "before_local_time",
          )
          .map((c) =>
            timeTemplates.find(
              (t) => t.type === c.type && t.time_hhmm === c.time_hhmm,
            )?.template_id,
          )
          .filter((id): id is string => id !== undefined),
      );
      for (const template of timeTemplates) {
        const row = document.createElement("label");
        row.className = "rules-check-row";
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "rules-template-pick";
        cb.value = template.template_id;
        cb.checked = selectedTemplateIds.has(template.template_id);
        const kind =
          template.type === "after_local_time" ? "After" : "Before";
        row.append(
          cb,
          document.createTextNode(` ${template.label} (${kind} ${template.time_hhmm})`),
        );
        timeField.append(row);
      }
    }
    conditionsField.append(timeField);
    form.append(conditionsField);

    const accuracyInput = document.createElement("input");
    accuracyInput.type = "number";
    accuracyInput.min = "1";
    accuracyInput.step = "1";
    accuracyInput.value = String(existing?.min_fix_accuracy_m ?? DEFAULT_MIN_FIX_ACCURACY_M);
    appendLabeledField(
      form,
      createFieldLabel("Min location accuracy (meters)", {
        detail:
          "Ignore GPS fixes whose horizontal accuracy is worse than this threshold. Prevents a fuzzy phone fix from falsely placing someone inside a geofence.",
        example:
          "50 m drops a ±120 m fix in a parking lot while still accepting a ±12 m fix in the driveway.",
      }),
      accuracyInput,
    );

    const cooldownInput = document.createElement("input");
    cooldownInput.type = "number";
    cooldownInput.min = "0";
    cooldownInput.value = String(existing?.cooldown_s ?? 300);
    appendLabeledField(
      form,
      createFieldLabel("Cooldown (seconds)", {
        detail:
          "Minimum wait before the same rule can fire again after a successful run.",
        example:
          "300 prevents the garage from re-opening every poll when both people linger on the fence edge.",
      }),
      cooldownInput,
    );

    const notifyField = document.createElement("fieldset");
    notifyField.className = "rules-editor-fieldset";
    const notifyLegend = document.createElement("legend");
    notifyLegend.textContent = "Email notification";
    notifyField.append(notifyLegend);
    const notifyRow = document.createElement("label");
    notifyRow.className = "rules-check-row";
    const notifyCb = document.createElement("input");
    notifyCb.type = "checkbox";
    notifyCb.checked = existing?.notify_on_fire ?? false;
    notifyRow.append(
      notifyCb,
      document.createTextNode(" Send email when this rule fires"),
    );
    notifyField.append(notifyRow);
    const notifyEmail = document.createElement("input");
    notifyEmail.type = "email";
    notifyEmail.placeholder = "you@example.com";
    notifyEmail.value = existing?.notification_email ?? "";
    notifyEmail.disabled = !notifyCb.checked;
    notifyCb.addEventListener("change", () => {
      notifyEmail.disabled = !notifyCb.checked;
    });
    appendLabeledField(
      notifyField,
      createFieldLabel("Notification recipient"),
      notifyEmail,
    );

    const actionsWrap = document.createElement("fieldset");
    actionsWrap.className = "rules-editor-fieldset";
    const actionsTitle = document.createElement("legend");
    actionsTitle.textContent = "Device actions";
    actionsWrap.append(actionsTitle);
    const deviceRows = this.appendDeviceActionGroups(
      actionsWrap,
      actionDevices,
      existing,
    );

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => {
      editor.remove();
    });
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Save rule";
    actions.append(cancelBtn, saveBtn);

    form.append(notifyField, actionsWrap, actions);

    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const participantIds = [...whoField.querySelectorAll<HTMLInputElement>("input:checked")].map(
        (el) => el.value,
      );
      if (participantIds.length === 0) {
        window.alert("Select at least one participant.");
        return;
      }
      const conditions: RuleOut["conditions"]["all"] = [
        {
          type: "participants_inside_geofence",
          geofence_id: whereSelect.value,
          participant_ids: participantIds,
        },
      ];
      if (sunsetCb.checked) {
        conditions.push({ type: "after_sunset", offset_minutes: 0 });
      }
      if (sunriseCb.checked) {
        conditions.push({ type: "before_sunrise", offset_minutes: 0 });
      }
      if (afterTime.value !== "") {
        conditions.push({ type: "after_local_time", time_hhmm: afterTime.value });
      }
      if (beforeTime.value !== "") {
        conditions.push({ type: "before_local_time", time_hhmm: beforeTime.value });
      }
      const selectedDays = dayPicker.getSelectedDays();
      if (selectedDays.length === 0) {
        window.alert("Select at least one day of the week.");
        return;
      }
      conditions.push({ type: "days_of_week", days: selectedDays });
      for (const cb of timeField.querySelectorAll<HTMLInputElement>(
        "input.rules-template-pick",
      )) {
        if (!cb.checked) {
          continue;
        }
        const template = timeTemplates.find((t) => t.template_id === cb.value);
        if (template !== undefined) {
          conditions.push(templateToCondition(template));
        }
      }
      const device_actions: RuleDeviceActionOut[] = deviceRows
        .filter((row) => row.checkbox.checked)
        .map((row) => ({
          family_id: row.device.family_id,
          device_id: row.device.device_id,
          action: row.actionSelect.value as RuleActionType,
        }));
      if (device_actions.length === 0) {
        window.alert("Select at least one device action.");
        return;
      }
      if (notifyCb.checked && notifyEmail.value.trim() === "") {
        window.alert("Enter a notification email or disable email notification.");
        return;
      }
      const rule: RuleOut = {
        id: existing?.id ?? slugifyId(idInput.value || labelInput.value),
        label: labelInput.value.trim(),
        enabled: existing?.enabled ?? false,
        trigger: "edge_true",
        cooldown_s: Number(cooldownInput.value) || 300,
        min_fix_accuracy_m: Number(accuracyInput.value) || DEFAULT_MIN_FIX_ACCURACY_M,
        notify_on_fire: notifyCb.checked,
        notification_email: notifyCb.checked ? notifyEmail.value.trim() : null,
        conditions: { all: conditions },
        device_actions,
      };
      void this.dataSource.saveRule(rule).then(() => {
        editor.remove();
        void this.refresh();
      });
    });

    editor.append(form);
    this.body.append(editor);
    labelInput.focus();
  }

  private appendParticipantMiniMap(
    card: HTMLElement,
    geofences: GeofenceOut[],
    participant: {
      display_name: string;
      last_fix: ParticipantFixOut | null;
    },
  ): void {
    const mapEl = document.createElement("div");
    mapEl.className = "rules-presence-mini-map";
    card.append(mapEl);
    const fallback = geofences.find((g) => g.enabled);
    const fix = participant.last_fix;
    if (fix === null && fallback === undefined) {
      return;
    }
    if (fix !== null) {
      mountPresenceMiniMap(mapEl, {
        accuracy_m: fix.accuracy_m,
        geofences,
        label: participant.display_name,
        lat: fix.lat,
        lon: fix.lon,
      });
      return;
    }
    if (fallback !== undefined) {
      mountPresenceMiniMap(mapEl, {
        accuracy_m: null,
        geofences,
        label: "No location yet",
        lat: fallback.center_lat,
        lon: fallback.center_lon,
      });
    }
  }

  private async refresh(): Promise<void> {
    this.status = await this.dataSource.getStatus();
    this.renderBody();
    this.syncTabUi();
  }

  private renderBody(): void {
    this.body.replaceChildren();
    if (this.status === null) {
      return;
    }
    switch (this.activeTab) {
      case "status":
        this.renderStatusTab(this.status);
        break;
      case "conditions":
        void this.renderConditionsTab(this.status);
        break;
      case "rules":
        void this.renderRulesTab();
        break;
      case "geofences":
        void this.renderGeofencesTab();
        break;
      case "mail":
        void this.renderMailTab();
        break;
      case "participants":
        void this.renderParticipantsTab();
        break;
    }
  }

  private async renderMailTab(): Promise<void> {
    const mount = document.createElement("div");
    mount.className = "rules-mail-mount";
    this.body.append(mount);
    const { mountMailSettingsPanel } = await import("./mail-settings-panel.js");
    await mountMailSettingsPanel(mount, this.dataSource);
  }

  private async renderConditionsTab(status: RulesStatusOut): Promise<void> {
    const templates = await this.dataSource.listTimeConditionTemplates();
    const sunsetMsg = sunsetStatusMessage(status.sun);
    const sunriseMsg = sunriseStatusMessage(status.sun);

    const dynamicHeading = document.createElement("h3");
    dynamicHeading.className = "rules-section-title";
    dynamicHeading.textContent = "Astronomical (dynamic)";

    const sunsetCard = document.createElement("article");
    sunsetCard.className = "rules-card";
    sunsetCard.innerHTML = `<p class="rules-card-meta"><span class="rules-dynamic-badge">${sunsetMsg.dynamicLabel}</span></p><p>${sunsetMsg.primary}</p><p class="rules-card-meta">Recalculated from home location daily. Enable per rule as <strong>After sunset</strong>.</p>`;

    const sunriseCard = document.createElement("article");
    sunriseCard.className = "rules-card";
    sunriseCard.innerHTML = `<p class="rules-card-meta"><span class="rules-dynamic-badge">${sunriseMsg.dynamicLabel}</span></p><p>${sunriseMsg.primary}</p><p class="rules-card-meta">Recalculated from home location daily. Enable per rule as <strong>Before sunrise</strong>.</p>`;

    const clockHeading = document.createElement("h3");
    clockHeading.className = "rules-section-title";
    clockHeading.textContent = "Clock-time templates";
    const clockLead = document.createElement("p");
    clockLead.className = "settings-dialog-lead";
    clockLead.textContent =
      "Fixed local times complement sunset/sunrise. Save templates here, then enable them per rule.";

    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const template of templates) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const title = document.createElement("strong");
      const kind = template.type === "after_local_time" ? "After" : "Before";
      title.textContent = `${template.label} (${kind} ${template.time_hhmm})`;
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-danger";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        if (window.confirm(`Delete template "${template.label}"?`)) {
          void this.dataSource
            .deleteTimeConditionTemplate(template.template_id)
            .then(() => this.refresh());
        }
      });
      row.append(title, delBtn);
      card.append(row);
      list.append(card);
    }

    const form = document.createElement("form");
    form.className = "rules-participant-form";
    const labelInput = document.createElement("input");
    labelInput.required = true;
    labelInput.placeholder = "Weekend late night";
    appendLabeledField(form, createFieldLabel("Template label"), labelInput);
    const typeSelect = document.createElement("select");
    for (const [value, label] of [
      ["after_local_time", "After"],
      ["before_local_time", "Before"],
    ] as const) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      typeSelect.append(opt);
    }
    appendLabeledField(form, createFieldLabel("Kind"), typeSelect);
    const timeInput = document.createElement("input");
    timeInput.type = "time";
    timeInput.required = true;
    appendLabeledField(form, createFieldLabel("Local time"), timeInput);
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Add template";
    form.append(saveBtn);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const template: TimeConditionTemplateOut = {
        template_id: slugifyId(labelInput.value),
        label: labelInput.value.trim(),
        type: typeSelect.value as TimeConditionTemplateOut["type"],
        time_hhmm: timeInput.value,
      };
      void this.dataSource.saveTimeConditionTemplate(template).then(() => {
        form.reset();
        void this.refresh();
      });
    });

    this.body.append(
      dynamicHeading,
      sunsetCard,
      sunriseCard,
      clockHeading,
      clockLead,
      list,
      form,
    );
  }

  private renderStatusTab(status: RulesStatusOut): void {
    const sunMsg = sunStatusMessage(status.sun);
    const sunBtn = document.createElement("button");
    sunBtn.type = "button";
    sunBtn.className = "rules-status-sun rules-clickable-card";
    const sunPrimary = document.createElement("span");
    sunPrimary.textContent = sunMsg.primary;
    const sunBadge = document.createElement("span");
    sunBadge.className = "rules-dynamic-badge";
    sunBadge.textContent = sunMsg.dynamicLabel;
    sunBtn.append(sunPrimary, sunBadge);
    sunBtn.addEventListener("click", () => {
      void this.setTab("conditions");
    });

    const participantsHeading = document.createElement("h3");
    participantsHeading.className = "rules-section-title";
    participantsHeading.textContent = "Participants";
    const participantList = document.createElement("div");
    participantList.className = "rules-card-list";
    for (const p of status.participants) {
      const card = document.createElement("article");
      card.className = "rules-card rules-clickable-card rules-participant-card";
      card.tabIndex = 0;
      card.setAttribute("role", "button");
      const name = document.createElement("strong");
      name.textContent = p.display_name;
      const deviceMeta = document.createElement("p");
      deviceMeta.className = "rules-card-meta";
      deviceMeta.textContent = `Tracking device: ${p.tracking_device_label}`;
      const meta = document.createElement("p");
      meta.className = "rules-card-meta";
      const inside =
        p.inside_geofence_ids.length > 0
          ? `Inside ${p.inside_geofence_ids.join(", ")}`
          : "Outside all geofences";
      meta.textContent = `${formatAge(p.age_seconds)} · ${inside}`;
      card.append(name, deviceMeta, meta);
      if (p.last_fix !== null) {
        const coords = document.createElement("p");
        coords.className = "rules-card-meta";
        const accuracy =
          p.last_fix.accuracy_m === null ? "unknown" : `±${p.last_fix.accuracy_m} m`;
        coords.textContent = `${p.last_fix.lat.toFixed(5)}, ${p.last_fix.lon.toFixed(5)} · ${accuracy}`;
        card.append(coords);
        if (
          p.last_fix.accuracy_m !== null
          && p.last_fix.accuracy_m > DEFAULT_MIN_FIX_ACCURACY_M
        ) {
          const warn = document.createElement("p");
          warn.className = "rules-card-warn";
          warn.textContent = `Low accuracy — ignored by rules (>${DEFAULT_MIN_FIX_ACCURACY_M} m)`;
          card.append(warn);
        }
      }
      this.appendParticipantMiniMap(card, status.geofences, {
        display_name: p.display_name,
        last_fix: p.last_fix,
      });
      const openParticipants = (): void => {
        void this.setTab("participants");
      };
      card.addEventListener("click", openParticipants);
      card.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openParticipants();
        }
      });
      participantList.append(card);
    }

    const rulesHeading = document.createElement("h3");
    rulesHeading.className = "rules-section-title";
    rulesHeading.textContent = "Rules";
    const ruleList = document.createElement("div");
    ruleList.className = "rules-card-list";
    for (const rule of status.rules) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const nameBtn = document.createElement("button");
      nameBtn.type = "button";
      nameBtn.className = "rules-card-title-btn";
      nameBtn.textContent = rule.label;
      nameBtn.addEventListener("click", () => {
        void this.dataSource.getRule(rule.id).then((full) => {
          if (full !== null) {
            void this.setTab("rules").then(() => {
              void this.openRuleEditor(full);
            });
          }
        });
      });
      const enableToggle = createEnableToggle(rule.enabled, (next) => {
        void this.dataSource
          .setRuleEnabled(rule.id, next)
          .then(() => this.refresh());
      });
      row.append(nameBtn, enableToggle);
      const meta = document.createElement("p");
      meta.className = "rules-card-meta";
      const met = rule.condition_currently_true ? "conditions met" : "conditions not met";
      const fired = rule.last_fired_at
        ? ` · last fired ${formatAge(
            Math.floor((Date.now() - Date.parse(rule.last_fired_at)) / 1000),
          )}`
        : "";
      meta.textContent = `${met}${fired}`;
      card.append(row, meta);
      void this.dataSource.getRule(rule.id).then((full) => {
        if (full?.notify_on_fire && full.notification_email !== null) {
          const notifyMeta = document.createElement("p");
          notifyMeta.className = "rules-card-meta";
          notifyMeta.textContent = `Email on fire → ${full.notification_email}`;
          card.append(notifyMeta);
        }
      });

      const condList = document.createElement("ul");
      condList.className = "rules-condition-list";
      for (const cond of rule.conditions) {
        const li = document.createElement("li");
        li.className = cond.met ? "rules-condition-met" : "rules-condition-unmet";
        li.textContent = `${cond.met ? "✓" : "✗"} ${cond.label}: ${cond.detail}`;
        condList.append(li);
      }
      card.append(condList);
      ruleList.append(card);
    }

    this.body.append(sunBtn, participantsHeading, participantList, rulesHeading, ruleList);
  }

  private async renderRulesTab(): Promise<void> {
    const rules = await this.dataSource.listRules();
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "btn";
    addBtn.textContent = "Add rule";
    addBtn.addEventListener("click", () => {
      void this.openRuleEditor(null);
    });
    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const rule of rules) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const title = document.createElement("strong");
      title.textContent = rule.label;
      const actions = document.createElement("div");
      actions.className = "rules-inline-actions";
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn btn-secondary";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => {
        void this.openRuleEditor(rule);
      });
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-danger";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        if (window.confirm(`Delete rule "${rule.label}"?`)) {
          void this.dataSource.deleteRule(rule.id).then(() => this.refresh());
        }
      });
      actions.append(editBtn, delBtn);
      row.append(title, actions);
      const meta = document.createElement("p");
      meta.className = "rules-card-meta";
      const actionSummary = rule.device_actions
        .map((a) => `${formatActionLabel(a.action)} ${a.device_id}`)
        .join(", ");
      const notify =
        rule.notify_on_fire && rule.notification_email !== null
          ? ` · email ${rule.notification_email}`
          : "";
      meta.textContent = `${rule.id} · ${rule.enabled ? "enabled" : "disabled"}${notify} · ${actionSummary}`;
      card.append(row, meta);
      list.append(card);
    }
    this.body.append(addBtn, list);
  }

  private async renderGeofencesTab(): Promise<void> {
    const mount = document.createElement("div");
    mount.className = "rules-geofence-mount";
    mount.dataset.testid = "rules-geofence-mount";
    this.body.append(mount);
    const { mountGeofenceMapPanel } = await import("./geofence-map.js");
    await mountGeofenceMapPanel(mount, this.dataSource, () => this.refresh());
  }

  private async renderParticipantsTab(): Promise<void> {
    const participants = await this.dataSource.listParticipants();
    const geofences = await this.dataSource.listGeofences();
    const sync = await this.dataSource.getMyTracksParticipantsSync();
    const status = this.status;

    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead";
    lead.textContent =
      "Participants are synced from my-tracks. Presence updates arrive via webhook; edit people in my-tracks, then sync here.";

    const syncRow = document.createElement("div");
    syncRow.className = "rules-participants-sync";
    const syncMeta = document.createElement("p");
    syncMeta.className = "rules-card-meta";
    const syncedAt =
      sync.last_synced_at === null
        ? "never"
        : formatAge(
            Math.max(
              0,
              Math.floor((Date.now() - Date.parse(sync.last_synced_at)) / 1000),
            ),
          );
    syncMeta.textContent = `${sync.participant_count} participants · last synced ${syncedAt}`;
    const syncBtn = document.createElement("button");
    syncBtn.type = "button";
    syncBtn.className = "btn btn-secondary";
    syncBtn.textContent = "Sync from my-tracks";
    syncBtn.addEventListener("click", () => {
      void this.dataSource.syncParticipantsFromMyTracks().then(() => this.refresh());
    });
    syncRow.append(syncMeta, syncBtn);

    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const p of participants) {
      const card = document.createElement("article");
      card.className = "rules-card rules-participant-card";
      const name = document.createElement("strong");
      name.textContent = `${p.display_name} (${p.participant_id})`;
      const deviceMeta = document.createElement("p");
      deviceMeta.className = "rules-card-meta";
      deviceMeta.textContent = `Tracking device: ${p.tracking_device_label}`;
      card.append(name, deviceMeta);
      const fix =
        status?.participants.find(
          (row): row is ParticipantStatusOut =>
            row.participant_id === p.participant_id,
        )?.last_fix ?? null;
      this.appendParticipantMiniMap(card, geofences, {
        display_name: p.display_name,
        last_fix: fix,
      });
      list.append(card);
    }

    this.body.append(lead, syncRow, list);
  }

  private async setTab(tab: RulesTabId): Promise<void> {
    this.activeTab = tab;
    await this.refresh();
  }

  private syncTabUi(): void {
    const tabs = this.panel.querySelectorAll<HTMLButtonElement>(".rules-tab");
    for (const tab of tabs) {
      const id = tab.dataset.tab as RulesTabId;
      const selected = id === this.activeTab;
      tab.classList.toggle("rules-tab-active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
    }
  }
}

export async function openAutomationsHubDialog(): Promise<void> {
  const dataSource = await createRulesDataSource();
  const hub = new RulesHubController(dataSource);
  await hub.open();
}

/** @deprecated Use openAutomationsHubDialog */
export async function openRulesHubDialog(): Promise<void> {
  await openAutomationsHubDialog();
}
