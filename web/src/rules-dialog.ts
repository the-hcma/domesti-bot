// Desktop Rules hub — Status, Rules, Geofences, Participants (mock-backed).

import type { RulesDataSource } from "./rules-data-source.js";
import { createRulesDataSource } from "./rules-data-source.js";
import type {
  GeofenceOut,
  ParticipantOut,
  RuleActionOut,
  RuleActionType,
  RuleOut,
  RulesStatusOut,
} from "./types.js";

type RulesTabId = "status" | "rules" | "geofences" | "participants";

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

class RulesHubController {
  private readonly dialog: HTMLDialogElement;
  private readonly panel: HTMLDivElement;
  private readonly body: HTMLDivElement;
  private readonly mockPill: HTMLSpanElement;
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

  private async refresh(): Promise<void> {
    this.status = await this.dataSource.getStatus();
    this.renderBody();
    this.syncTabUi();
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
    const sun = document.createElement("p");
    sun.className = "rules-status-sun";
    sun.textContent = status.sun.is_dark
      ? `Dark now — sunset was ${formatLocalTime(status.sun.sunset_at)}`
      : `Light until ${formatLocalTime(status.sun.sunset_at)}`;

    const participantsHeading = document.createElement("h3");
    participantsHeading.className = "rules-section-title";
    participantsHeading.textContent = "Participants";
    const participantList = document.createElement("div");
    participantList.className = "rules-card-list";
    for (const p of status.participants) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const name = document.createElement("strong");
      name.textContent = p.display_name;
      const meta = document.createElement("p");
      meta.className = "rules-card-meta";
      const inside =
        p.inside_geofence_ids.length > 0
          ? `Inside ${p.inside_geofence_ids.join(", ")}`
          : "Outside all geofences";
      meta.textContent = `${formatAge(p.age_seconds)} · ${inside}`;
      if (p.last_fix !== null) {
        const coords = document.createElement("p");
        coords.className = "rules-card-meta";
        coords.textContent = `${p.last_fix.lat.toFixed(5)}, ${p.last_fix.lon.toFixed(5)}`;
        card.append(name, meta, coords);
      } else {
        card.append(name, meta);
      }
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
      const name = document.createElement("strong");
      name.textContent = rule.label;
      const toggle = document.createElement("input");
      toggle.type = "checkbox";
      toggle.checked = rule.enabled;
      toggle.addEventListener("change", () => {
        void this.dataSource
          .setRuleEnabled(rule.id, toggle.checked)
          .then(() => this.refresh());
      });
      row.append(name, toggle);
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
      ruleList.append(card);
    }

    this.body.append(sun, participantsHeading, participantList, rulesHeading, ruleList);
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
      meta.textContent = `${rule.id} · ${rule.enabled ? "enabled" : "disabled"}`;
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
    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const p of participants) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const name = document.createElement("strong");
      name.textContent = `${p.display_name} (${p.participant_id})`;
      row.append(name);
      card.append(row);
      list.append(card);
    }

    const form = document.createElement("form");
    form.className = "rules-participant-form";
    const idField = document.createElement("label");
    idField.className = "settings-dialog-field";
    idField.innerHTML = "<span>Participant id</span>";
    const idInput = document.createElement("input");
    idInput.name = "participant_id";
    idInput.required = true;
    idInput.pattern = "[a-z0-9_-]+";
    idField.append(idInput);
    const nameField = document.createElement("label");
    nameField.className = "settings-dialog-field";
    nameField.innerHTML = "<span>Display name</span>";
    const nameInput = document.createElement("input");
    nameInput.name = "display_name";
    nameInput.required = true;
    nameField.append(nameInput);
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Add participant";
    form.append(idField, nameField, saveBtn);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const participant: ParticipantOut = {
        participant_id: idInput.value.trim(),
        display_name: nameInput.value.trim(),
        enabled: true,
      };
      void this.dataSource.saveParticipant(participant).then(() => {
        form.reset();
        void this.refresh();
      });
    });

    const webhook = document.createElement("p");
    webhook.className = "settings-dialog-lead";
    webhook.textContent =
      "my-tracks relays fixes via POST /v1/webhooks/presence with participant_id matching the id slug above.";

    this.body.append(list, form, webhook);
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
      row.append(cb, document.createTextNode(` ${p.display_name}`));
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

    const sunsetRow = document.createElement("label");
    sunsetRow.className = "rules-check-row";
    const sunsetCb = document.createElement("input");
    sunsetCb.type = "checkbox";
    sunsetCb.checked =
      existing?.conditions.all.some((c) => c.type === "after_sunset") ?? true;
    sunsetRow.append(sunsetCb, document.createTextNode(" Only after sunset"));

    const cooldownField = document.createElement("label");
    cooldownField.className = "settings-dialog-field";
    cooldownField.innerHTML = "<span>Cooldown (seconds)</span>";
    const cooldownInput = document.createElement("input");
    cooldownInput.type = "number";
    cooldownInput.min = "0";
    cooldownInput.value = String(existing?.cooldown_s ?? 300);
    cooldownField.append(cooldownInput);

    const actionsWrap = document.createElement("div");
    actionsWrap.className = "rules-editor-actions";
    const actionsTitle = document.createElement("h3");
    actionsTitle.className = "rules-section-title";
    actionsTitle.textContent = "Actions";
    actionsWrap.append(actionsTitle);

    const actionRows: HTMLDivElement[] = [];
    const initialActions: RuleActionOut[] =
      existing?.actions ?? [
        { type: "turn_on", targets: [{ family_id: "kasa", device_id: "192.168.1.42" }] },
      ];

    const addActionRow = (action?: RuleActionOut): void => {
      const row = document.createElement("div");
      row.className = "rules-action-row";
      const typeSelect = document.createElement("select");
      for (const t of ["turn_on", "turn_off", "open", "close"] as RuleActionType[]) {
        const opt = document.createElement("option");
        opt.value = t;
        opt.textContent = t.replace("_", " ");
        typeSelect.append(opt);
      }
      typeSelect.value = action?.type ?? "turn_on";
      const deviceSelect = document.createElement("select");
      for (const d of actionDevices) {
        const opt = document.createElement("option");
        opt.value = `${d.family_id}\0${d.device_id}`;
        opt.textContent = `${d.label} (${d.family_id})`;
        deviceSelect.append(opt);
      }
      if (action?.targets[0]) {
        deviceSelect.value = `${action.targets[0].family_id}\0${action.targets[0].device_id}`;
      }
      row.append(typeSelect, deviceSelect);
      actionRows.push(row);
      actionsWrap.append(row);
    };
    for (const action of initialActions) {
      addActionRow(action);
    }
    const addActionBtn = document.createElement("button");
    addActionBtn.type = "button";
    addActionBtn.className = "btn btn-secondary";
    addActionBtn.textContent = "Add action";
    addActionBtn.addEventListener("click", () => {
      addActionRow();
    });
    actionsWrap.append(addActionBtn);

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
      sunsetRow,
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
      if (sunsetCb.checked) {
        conditions.push({ type: "after_sunset", offset_minutes: 0 });
      }
      const ruleActions: RuleActionOut[] = actionRows.map((row) => {
        const type = row.querySelector<HTMLSelectElement>("select")?.value as RuleActionType;
        const deviceKey = row.querySelectorAll<HTMLSelectElement>("select")[1]?.value ?? "";
        const [family_id, device_id] = deviceKey.split("\0");
        return {
          type,
          targets: [{ family_id: family_id ?? "kasa", device_id: device_id ?? "" }],
        };
      });
      const rule: RuleOut = {
        id: existing?.id ?? slugifyId(idInput.value || labelInput.value),
        label: labelInput.value.trim(),
        enabled: existing?.enabled ?? false,
        trigger: "edge_true",
        cooldown_s: Number(cooldownInput.value) || 300,
        conditions: { all: conditions },
        actions: ruleActions,
      };
      void this.dataSource.saveRule(rule).then(() => {
        editor.remove();
        void this.refresh();
      });
    });

    editor.append(form);
    this.body.append(editor);
  }
}

export async function openRulesHubDialog(): Promise<void> {
  const dataSource = await createRulesDataSource();
  const hub = new RulesHubController(dataSource);
  await hub.open();
}
