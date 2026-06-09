// Desktop Rules hub — Status, Rules, Geofences, Participants (mock-backed).

import type { RulesDataSource } from "./rules-data-source.js";
import { createRulesDataSource } from "./rules-data-source.js";
import { DEFAULT_MIN_FIX_ACCURACY_M } from "./rules-evaluate.js";
import type {
  GeofenceOut,
  ParticipantOut,
  RuleActionDeviceOut,
  RuleActionType,
  RuleDeviceActionOut,
  RuleOut,
  RulesStatusOut,
  RulesSunOut,
  UIDeviceKind,
} from "./types.js";

type RulesTabId = "status" | "rules" | "geofences" | "participants";

type SunConditionMode = "after_sunset" | "before_sunrise" | "none";

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

function slugifyId(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function sunConditionModeFromRule(rule: RuleOut | null): SunConditionMode {
  if (rule === null) {
    return "after_sunset";
  }
  if (rule.conditions.all.some((c) => c.type === "before_sunrise")) {
    return "before_sunrise";
  }
  if (rule.conditions.all.some((c) => c.type === "after_sunset")) {
    return "after_sunset";
  }
  return "none";
}

function sunStatusMessage(sun: RulesSunOut): { dynamicLabel: string; primary: string } {
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
    this.dialog.className = "settings-dialog rules-dialog";
    this.panel = document.createElement("div");
    this.panel.className = "settings-dialog-panel";
    const header = document.createElement("header");
    header.className = "settings-dialog-header rules-dialog-header";
    const titleWrap = document.createElement("div");
    titleWrap.className = "rules-dialog-title-wrap";
    const title = document.createElement("h2");
    title.textContent = "Rules";
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
      ["rules", "Rules"],
      ["geofences", "Geofences"],
      ["participants", "Participants"],
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
    await this.refresh();
    this.dialog.showModal();
  }

  private openParticipantEditor(existing: ParticipantOut | null): void {
    const editor = document.createElement("div");
    editor.className = "rules-editor-panel";
    const form = document.createElement("form");
    form.className = "rules-editor-form";

    const idField = document.createElement("label");
    idField.className = "settings-dialog-field";
    idField.innerHTML = "<span>Participant id</span>";
    const idInput = document.createElement("input");
    idInput.value = existing?.participant_id ?? "";
    idInput.required = true;
    idInput.pattern = "[a-z0-9_-]+";
    idInput.readOnly = existing !== null;
    idField.append(idInput);

    const nameField = document.createElement("label");
    nameField.className = "settings-dialog-field";
    nameField.innerHTML = "<span>Display name</span>";
    const nameInput = document.createElement("input");
    nameInput.value = existing?.display_name ?? "";
    nameInput.required = true;
    nameField.append(nameInput);

    const deviceField = document.createElement("label");
    deviceField.className = "settings-dialog-field";
    deviceField.innerHTML =
      "<span>Tracking device</span><small class=\"rules-field-hint\">Phone or tracker that reports this person's location via my-tracks</small>";
    const deviceInput = document.createElement("input");
    deviceInput.value = existing?.tracking_device_label ?? "";
    deviceInput.required = true;
    deviceInput.placeholder = "Henrique's iPhone";
    deviceField.append(deviceInput);

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
    saveBtn.textContent = existing === null ? "Add participant" : "Save participant";
    actions.append(cancelBtn, saveBtn);

    form.append(idField, nameField, deviceField, actions);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const participant: ParticipantOut = {
        participant_id: idInput.value.trim(),
        display_name: nameInput.value.trim(),
        tracking_device_label: deviceInput.value.trim(),
        enabled: existing?.enabled ?? true,
      };
      void this.dataSource.saveParticipant(participant).then(() => {
        editor.remove();
        void this.refresh();
      });
    });

    editor.append(form);
    this.body.append(editor);
    nameInput.focus();
  }

  private async openRuleEditor(existing: RuleOut | null): Promise<void> {
    const geofences = await this.dataSource.listGeofences();
    const participants = await this.dataSource.listParticipants();
    const actionDevices = await this.dataSource.listActionDevices();

    const editor = document.createElement("div");
    editor.className = "rules-editor-panel";
    const form = document.createElement("form");
    form.className = "rules-editor-form";

    const labelField = document.createElement("label");
    labelField.className = "settings-dialog-field";
    labelField.innerHTML = "<span>Name</span>";
    const labelInput = document.createElement("input");
    labelInput.value = existing?.label ?? "";
    labelInput.required = true;
    labelField.append(labelInput);

    const idField = document.createElement("label");
    idField.className = "settings-dialog-field";
    idField.innerHTML = "<span>Rule id</span>";
    const idInput = document.createElement("input");
    idInput.value = existing?.id ?? "";
    idInput.required = true;
    idInput.readOnly = existing !== null;
    idField.append(idInput);

    const whoField = document.createElement("fieldset");
    whoField.className = "rules-editor-fieldset";
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

    const whereField = document.createElement("label");
    whereField.className = "settings-dialog-field";
    whereField.innerHTML = "<span>Geofence</span>";
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
    whereField.append(whereSelect);

    const sunField = document.createElement("label");
    sunField.className = "settings-dialog-field";
    sunField.innerHTML =
      "<span>Time of day (dynamic)</span><small class=\"rules-field-hint\">Evaluated from home location sunset/sunrise</small>";
    const sunSelect = document.createElement("select");
    for (const [value, label] of [
      ["after_sunset", "After sunset"],
      ["before_sunrise", "Before sunrise"],
      ["none", "Any time of day"],
    ] as const) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      sunSelect.append(opt);
    }
    sunSelect.value = sunConditionModeFromRule(existing);
    sunField.append(sunSelect);

    const accuracyField = document.createElement("label");
    accuracyField.className = "settings-dialog-field";
    accuracyField.innerHTML =
      "<span>Min location accuracy (meters)</span><small class=\"rules-field-hint\">Ignore fixes with horizontal accuracy worse than this</small>";
    const accuracyInput = document.createElement("input");
    accuracyInput.type = "number";
    accuracyInput.min = "1";
    accuracyInput.step = "1";
    accuracyInput.value = String(existing?.min_fix_accuracy_m ?? DEFAULT_MIN_FIX_ACCURACY_M);
    accuracyField.append(accuracyInput);

    const cooldownField = document.createElement("label");
    cooldownField.className = "settings-dialog-field";
    cooldownField.innerHTML = "<span>Cooldown (seconds)</span>";
    const cooldownInput = document.createElement("input");
    cooldownInput.type = "number";
    cooldownInput.min = "0";
    cooldownInput.value = String(existing?.cooldown_s ?? 300);
    cooldownField.append(cooldownInput);

    const actionsWrap = document.createElement("fieldset");
    actionsWrap.className = "rules-editor-fieldset";
    const actionsTitle = document.createElement("legend");
    actionsTitle.textContent = "Device actions";
    actionsWrap.append(actionsTitle);

    const deviceActionState = new Map<string, RuleDeviceActionOut>();
    for (const entry of existing?.device_actions ?? []) {
      deviceActionState.set(`${entry.family_id}\0${entry.device_id}`, entry);
    }

    const deviceRows: {
      actionSelect: HTMLSelectElement;
      checkbox: HTMLInputElement;
      device: RuleActionDeviceOut;
    }[] = [];

    for (const device of actionDevices) {
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
      name.textContent = `${device.label} (${device.family_id})`;
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
      actionsWrap.append(row);
      deviceRows.push({ actionSelect, checkbox, device });
    }

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

    form.append(
      labelField,
      idField,
      whoField,
      whereField,
      sunField,
      accuracyField,
      cooldownField,
      actionsWrap,
      actions,
    );

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
      if (sunSelect.value === "after_sunset") {
        conditions.push({ type: "after_sunset", offset_minutes: 0 });
      } else if (sunSelect.value === "before_sunrise") {
        conditions.push({ type: "before_sunrise", offset_minutes: 0 });
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
      const rule: RuleOut = {
        id: existing?.id ?? slugifyId(idInput.value || labelInput.value),
        label: labelInput.value.trim(),
        enabled: existing?.enabled ?? false,
        trigger: "edge_true",
        cooldown_s: Number(cooldownInput.value) || 300,
        min_fix_accuracy_m: Number(accuracyInput.value) || DEFAULT_MIN_FIX_ACCURACY_M,
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

  private openSunInfoDialog(sun: RulesSunOut): void {
    const overlay = document.createElement("div");
    overlay.className = "rules-inline-dialog";
    const panel = document.createElement("div");
    panel.className = "rules-inline-dialog-panel";
    const title = document.createElement("h3");
    title.className = "rules-section-title";
    title.textContent = "Dynamic sun conditions";
    const body = document.createElement("p");
    body.className = "settings-dialog-lead";
    body.textContent =
      "Sunset and sunrise times are computed from your home location. Rules can require after sunset or before sunrise as dynamic conditions.";
    const times = document.createElement("ul");
    times.className = "rules-sun-times";
    times.innerHTML = `<li>Sunset: ${formatLocalTime(sun.sunset_at)}</li><li>Sunrise: ${formatLocalTime(sun.sunrise_at)}</li><li>Currently: ${sun.is_dark ? "dark" : "daylight"}</li>`;
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "btn";
    closeBtn.textContent = "Close";
    closeBtn.addEventListener("click", () => {
      overlay.remove();
    });
    panel.append(title, body, times, closeBtn);
    overlay.append(panel);
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) {
        overlay.remove();
      }
    });
    this.body.append(overlay);
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
      case "rules":
        void this.renderRulesTab();
        break;
      case "geofences":
        void this.renderGeofencesTab();
        break;
      case "participants":
        void this.renderParticipantsTab();
        break;
    }
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
      this.openSunInfoDialog(status.sun);
    });

    const participantsHeading = document.createElement("h3");
    participantsHeading.className = "rules-section-title";
    participantsHeading.textContent = "Participants";
    const participantList = document.createElement("div");
    participantList.className = "rules-card-list";
    for (const p of status.participants) {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "rules-card rules-clickable-card";
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
      card.addEventListener("click", () => {
        void this.setTab("participants").then(() => {
          this.openParticipantEditor(p);
        });
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
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = rule.enabled;
      toggle.title = "Enable rule";
      toggle.addEventListener("click", (ev) => {
        ev.stopPropagation();
      });
      toggle.addEventListener("change", () => {
        void this.dataSource
          .setRuleEnabled(rule.id, toggle.checked)
          .then(() => this.refresh());
      });
      row.append(nameBtn, toggle);
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
      meta.textContent = `${rule.id} · ${rule.enabled ? "enabled" : "disabled"} · ${actionSummary}`;
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
    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "btn";
    addBtn.textContent = "Add participant";
    addBtn.addEventListener("click", () => {
      this.openParticipantEditor(null);
    });
    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const p of participants) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const name = document.createElement("strong");
      name.textContent = `${p.display_name} (${p.participant_id})`;
      const editBtn = document.createElement("button");
      editBtn.type = "button";
      editBtn.className = "btn btn-secondary";
      editBtn.textContent = "Edit";
      editBtn.addEventListener("click", () => {
        this.openParticipantEditor(p);
      });
      row.append(name, editBtn);
      const deviceMeta = document.createElement("p");
      deviceMeta.className = "rules-card-meta";
      deviceMeta.textContent = `Tracking device: ${p.tracking_device_label}`;
      card.append(row, deviceMeta);
      list.append(card);
    }

    const webhook = document.createElement("p");
    webhook.className = "settings-dialog-lead";
    webhook.textContent =
      "my-tracks relays fixes via POST /v1/webhooks/presence with participant_id matching the id slug above.";

    this.body.append(addBtn, list, webhook);
  }

  private async setTab(tab: RulesTabId): Promise<void> {
    this.activeTab = tab;
    this.dialog.classList.toggle("rules-dialog-wide", tab === "geofences");
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

export async function openRulesHubDialog(): Promise<void> {
  const dataSource = await createRulesDataSource();
  const hub = new RulesHubController(dataSource);
  await hub.open();
}
