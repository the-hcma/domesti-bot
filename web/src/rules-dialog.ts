// Desktop Automations hub — Status, Conditions, automations, geofences, mail.

import {
  AFTER_SUNSET_WINDOW_DESCRIPTION,
  afterSunsetStatusMessage,
  beforeSunriseStatusMessage,
  BEFORE_SUNRISE_WINDOW_DESCRIPTION,
  DAYLIGHT_WINDOW_DESCRIPTION,
  daylightStatusMessage,
} from "./astronomical-conditions.js";
import { runMyTracksSyncAction } from "./mytracks-sync-dialog.js";
import { appendMyTracksInstanceText } from "./mytracks-ui-helpers.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createRulesDataSource } from "./rules-data-source.js";
import { DEFAULT_MIN_LOCATION_ACCURACY_M } from "./rules-constants.js";
import {
  formatInsideGeofencesLine,
  mountPresenceMap,
  userStatusToMapUser,
  renderUserDetailText,
  type PresenceMapController,
} from "./presence-map.js";
import {
  buildInspectorContext,
  mountRuleInspectorPanel,
  type RuleInspectorMountOptions,
} from "./rule-inspector.js";
import { haversineM } from "./rules-mock-fixtures.js";
import {
  appendRuleSummaryBody,
  buildRuleSummaryContext,
  collectUserIdsFromRule,
  joinNames,
  summarizeRule,
} from "./rule-summary.js";
import {
  ALL_DAYS_OF_WEEK,
  createDayOfWeekPicker,
  createEnableToggle,
  createFieldLabel,
  FAMILY_ACTION_GROUP_LABELS,
  createBrokenRuleBadge,
  resolveRosterUser,
  appendRuleLastMetLine,
  ruleStatusHeadline,
  userDisplayLabel,
  preventBrowserAutofill,
} from "./rules-ui-helpers.js";
import { createAuditedTimeElement } from "./format-timestamp.js";
import { mountHomeLocationMapInset, type HomeLocationMapInset } from "./home-location-map-inset.js";
import { userMarkerColor } from "./map-device-colors.js";
import { confirmAction, showErrorToast } from "./ui-toast.js";
import {
  AstronomicalWindowBoundary,
  ConfirmButtonVariant,
  MyTracksSyncKind,
  RuleActionType,
  RuleConditionType,
  RuleTrigger,
  RulesTabId,
  UIDeviceKind,
  type GeofenceOut,
  type UserOut,
  type UserStatusOut,
  type RuleActionDeviceOut,
  type RuleConditionOut,
  type RuleDeviceActionOut,
  type RuleOut,
  type RuleStatusSummaryOut,
  type RulesStatusOut,
  type SettingsLocationOut,
  type TimeConditionTemplateOut,
} from "./types.js";


/** Match settings home coordinates to a geofence center (meters). */
const HOME_GEOFENCE_MATCH_TOLERANCE_M = 50;

export type AutomationsHubOpenOptions = {
  ruleId?: string;
  tab?: RulesTabId;
};

function actionOptionsForKind(kind: UIDeviceKind): RuleActionType[] {
  switch (kind) {
    case UIDeviceKind.Switch:
      return [RuleActionType.TurnOn, RuleActionType.TurnOff];
    case UIDeviceKind.Door:
      return [RuleActionType.Open, RuleActionType.Close];
    case UIDeviceKind.Speaker:
      return [RuleActionType.Pause, RuleActionType.Resume];
    case UIDeviceKind.Occupancy:
      // Occupancy sensors are read-only in the rules UI until EP1 actions land.
      return [];
  }
}

function appendAstronomicalConditionCard(
  dynamicLabel: string,
  primary: string,
  ruleEnableLabel: string,
  windowDescription: string,
  homeGeofence: GeofenceOut | null,
  settings: SettingsLocationOut,
  onHomeClick: (geofenceId: string | null) => void,
): HTMLElement {
  const card = document.createElement("article");
  card.className = "rules-card";
  const badgeRow = document.createElement("p");
  badgeRow.className = "rules-card-meta";
  const badge = document.createElement("span");
  badge.className = "rules-dynamic-badge";
  badge.textContent = dynamicLabel;
  badgeRow.append(badge);
  const primaryRow = document.createElement("p");
  primaryRow.textContent = primary;
  const windowRow = document.createElement("p");
  windowRow.className = "rules-card-meta";
  windowRow.textContent = windowDescription;
  card.append(badgeRow, primaryRow, windowRow);
  appendAstronomicalRecalcNote(
    card,
    homeGeofence,
    settings,
    ruleEnableLabel,
    onHomeClick,
  );
  return card;
}

function appendAstronomicalRecalcNote(
  card: HTMLElement,
  homeGeofence: GeofenceOut | null,
  settings: SettingsLocationOut,
  ruleEnableLabel: string,
  onHomeClick: (geofenceId: string | null) => void,
): void {
  const meta = document.createElement("p");
  meta.className = "rules-card-meta";
  meta.append(document.createTextNode("Recalculated from "));
  const linkLabel =
    homeGeofence?.label
    ?? (settings.home_label !== null && settings.home_label.trim() !== ""
      ? settings.home_label.trim()
      : "home location");
  const link = document.createElement("button");
  link.type = "button";
  link.className = "rules-inline-link";
  link.textContent = linkLabel;
  link.addEventListener("click", () => {
    onHomeClick(homeGeofence?.geofence_id ?? null);
  });
  meta.append(link);
  meta.append(document.createTextNode(" daily. Enable per rule as "));
  const strong = document.createElement("strong");
  strong.textContent = ruleEnableLabel;
  meta.append(strong, document.createTextNode("."));
  card.append(meta);
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

function hhmmToTimeInput(hhmm: string): string {
  if (/^\d{1,2}:\d{2}$/.test(hhmm)) {
    const [h, m] = hhmm.split(":");
    return `${h?.padStart(2, "0") ?? "00"}:${m ?? "00"}`;
  }
  return "";
}

function resolveHomeGeofence(
  geofences: GeofenceOut[],
  settings: SettingsLocationOut,
): GeofenceOut | null {
  if (settings.lat !== 0 || settings.lon !== 0) {
    for (const geofence of geofences) {
      if (!geofence.enabled) {
        continue;
      }
      const dist = haversineM(
        settings.lat,
        settings.lon,
        geofence.center_lat,
        geofence.center_lon,
      );
      if (dist <= HOME_GEOFENCE_MATCH_TOLERANCE_M) {
        return geofence;
      }
    }
  }
  if (settings.home_label !== null && settings.home_label.trim() !== "") {
    const normalized = settings.home_label.trim().toLowerCase();
    const byLabel = geofences.find(
      (geofence) =>
        geofence.enabled
        && geofence.label.trim().toLowerCase() === normalized,
    );
    if (byLabel !== undefined) {
      return byLabel;
    }
  }
  return geofences.find((geofence) => geofence.enabled) ?? null;
}

function slugifyId(raw: string): string {
  return raw
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function templateToCondition(
  template: TimeConditionTemplateOut,
): RuleConditionOut {
  return {
    type: RuleConditionType.LocalTimeWindow,
    start_hhmm: template.start_hhmm,
    end_hhmm: template.end_hhmm,
  };
}

class RulesHubController {
  private static readonly PRESENCE_POLL_MS = 5000;

  private readonly body: HTMLDivElement;
  private readonly dialog: HTMLDialogElement;
  private readonly panel: HTMLDivElement;
  private readonly titleWrap: HTMLDivElement;
  private sourcePill: HTMLSpanElement | null = null;
  private activeTab: RulesTabId = RulesTabId.Status;
  private dataSource: RulesDataSource;
  private pendingGeofenceFocusId: string | null = null;
  private pendingRuleInspectorId: string | null = null;
  private pendingStatusRuleInspectorId: string | null = null;
  private homeLocationMap: HomeLocationMapInset | null = null;
  private homeLocationMapDebounceTimer: ReturnType<typeof window.setTimeout> | null =
    null;
  private homeLocationMapGeneration = 0;
  private presenceMap: PresenceMapController | null = null;
  private presencePollTimer: ReturnType<typeof window.setInterval> | null = null;
  private status: RulesStatusOut | null = null;

  constructor(dataSource: RulesDataSource, options: AutomationsHubOpenOptions = {}) {
    this.dataSource = dataSource;
    const targetTab = options.tab ?? this.activeTab;
    this.activeTab = targetTab;
    if (options.ruleId !== undefined) {
      if (targetTab === RulesTabId.Status) {
        this.pendingStatusRuleInspectorId = options.ruleId;
      } else {
        this.pendingRuleInspectorId = options.ruleId;
      }
    }
    this.dialog = document.createElement("dialog");
    this.dialog.className = "settings-dialog rules-dialog automations-dialog";
    this.dialog.setAttribute("autocomplete", "off");
    this.panel = document.createElement("div");
    this.panel.className = "settings-dialog-panel";
    const header = document.createElement("header");
    header.className = "settings-dialog-header rules-dialog-header";
    this.titleWrap = document.createElement("div");
    this.titleWrap.className = "rules-dialog-title-wrap";
    const title = document.createElement("h2");
    title.textContent = "Automations";
    this.titleWrap.append(title);
    header.append(this.titleWrap, createDialogCloseButton(this.dialog));
    const tabBar = document.createElement("div");
    tabBar.className = "rules-tab-bar";
    tabBar.setAttribute("role", "tablist");
    for (const tab of [
      [RulesTabId.Status, "Status"],
      [RulesTabId.Conditions, "Conditions"],
      [RulesTabId.Rules, "Rules"],
      [RulesTabId.Geofences, "Geofences"],
      [RulesTabId.Users, "Users"],
      [RulesTabId.Vacation, "Vacation"],
      [RulesTabId.Mail, "Mail"],
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
      this.stopPresencePoll();
      this.clearHomeLocationMap();
      this.presenceMap?.destroy();
      this.presenceMap = null;
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
    delayInput: HTMLInputElement;
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
      delayInput: HTMLInputElement;
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
        const defaultAction = actionOptionsForKind(device.kind)[0] ?? RuleActionType.TurnOn;
        actionSelect.value = existingAction?.action ?? defaultAction;
        actionSelect.disabled = !checkbox.checked;
        const delayInput = document.createElement("input");
        delayInput.type = "number";
        delayInput.min = "0";
        delayInput.max = "86400";
        delayInput.step = "1";
        delayInput.placeholder = "delay s";
        delayInput.title =
          "Optional seconds after rule fire before this action runs (0 = immediate). Pending delays are saved and survive a server restart.";
        delayInput.className = "rules-device-action-delay";
        const existingDelay = existingAction?.delay_s;
        delayInput.value =
          existingDelay !== undefined && existingDelay !== null && existingDelay > 0
            ? String(existingDelay)
            : "";
        delayInput.disabled = !checkbox.checked;
        checkbox.addEventListener("change", () => {
          actionSelect.disabled = !checkbox.checked;
          delayInput.disabled = !checkbox.checked;
        });
        label.append(checkbox, name, actionSelect, delayInput);
        row.append(label);
        group.append(row);
        deviceRows.push({ actionSelect, checkbox, delayInput, device });
      }
      actionsWrap.append(group);
    }
    return deviceRows;
  }

  private mountUserPresenceMap(
    parent: HTMLElement,
    geofences: GeofenceOut[],
    users: UserStatusOut[],
    options: {
      compact?: boolean;
      includeUserIdInTooltip?: boolean;
      showLegend?: boolean;
      showUserFilters: boolean;
      showTextDetails: boolean;
    },
  ): void {
    const mount = document.createElement("div");
    mount.className =
      options.compact === true
        ? "rules-presence-map-mount rules-presence-map-mount-compact"
        : "rules-presence-map-mount";
    parent.append(mount);
    this.presenceMap = mountPresenceMap(mount, {
      compact: options.compact === true,
      geofences,
      users: users.map(userStatusToMapUser),
      showUserFilters: options.showUserFilters,
      ...(options.showLegend === false ? { showLegend: false as const } : {}),
      ...(options.includeUserIdInTooltip === true
        ? { includeUserIdInTooltip: true as const }
        : {}),
    });
    this.startPresencePoll();
    if (options.showTextDetails) {
      const details = document.createElement("div");
      details.className = "rules-user-details-list";
      for (const user of users) {
        details.append(
          renderUserDetailText(
            userStatusToMapUser(user),
            true,
            geofences,
          ),
        );
      }
      parent.append(details);
    }
  }

  private liveStatusForRule(ruleId: string): RuleStatusSummaryOut | undefined {
    return this.status?.rules.find((row) => row.id === ruleId);
  }

  private async navigateToRule(ruleId: string): Promise<void> {
    this.pendingRuleInspectorId = ruleId;
    await this.setTab(RulesTabId.Rules);
  }

  private async openRuleEditor(existing: RuleOut | null): Promise<void> {
    const geofences = await this.dataSource.listGeofences();
    const users = await this.dataSource.listUsers();
    const actionDevices = await this.dataSource.listActionDevices();
    const timeTemplates = await this.dataSource.listTimeConditionTemplates();

    const editor = document.createElement("div");
    editor.className = "rules-editor-panel";
    const form = document.createElement("form");
    form.className = "rules-editor-form";
    form.setAttribute("autocomplete", "off");

    let ruleEnabled = existing?.enabled ?? true;
    const editorHeader = document.createElement("div");
    editorHeader.className = "rules-rule-editor-header";
    const editorTitle = document.createElement("h3");
    editorTitle.className = "rules-rule-editor-title";
    editorTitle.textContent = existing === null ? "New rule" : "Edit rule";
    const enableToggle = createEnableToggle(ruleEnabled, (next) => {
      ruleEnabled = next;
    });
    editorHeader.append(editorTitle, enableToggle);
    form.append(editorHeader);

    const labelInput = document.createElement("input");
    labelInput.value = existing?.label ?? "";
    labelInput.required = true;
    preventBrowserAutofill(labelInput);
    appendLabeledField(
      form,
      createFieldLabel("Name"),
      labelInput,
    );

    const idInput = document.createElement("input");
    idInput.value = existing?.id ?? "";
    idInput.required = true;
    preventBrowserAutofill(idInput);
    appendLabeledField(
      form,
      createFieldLabel("Rule id"),
      idInput,
    );
    let idManuallyEdited = Boolean(existing?.id);
    labelInput.addEventListener("input", () => {
      if (!idManuallyEdited) {
        idInput.value = slugifyId(labelInput.value);
      }
    });
    idInput.addEventListener("input", (ev) => {
      if (ev.isTrusted) {
        idManuallyEdited = true;
      }
    });
    if (existing === null && idInput.value === "") {
      idInput.value = slugifyId(labelInput.value);
    }

    const conditionsField = document.createElement("fieldset");
    conditionsField.className = "rules-editor-fieldset";
    const conditionsLegend = document.createElement("legend");
    conditionsLegend.textContent = "Conditions";
    conditionsField.append(conditionsLegend);

    const whoField = document.createElement("fieldset");
    whoField.className = "rules-editor-fieldset rules-editor-subfieldset";
    const whoLegend = document.createElement("legend");
    whoField.append(whoLegend);
    const selectedUsers = new Set<string>(
      existing?.conditions.all.find((c) => c.type === RuleConditionType.UsersInsideGeofence)
        ?.user_ids ?? ["henrique", "kristen"],
    );
    const syncWhoLegend = (): void => {
      const names = users
        .filter((p) => selectedUsers.has(p.user_id))
        .map((p) =>
          userDisplayLabel(p.user_id, p.display_name),
        );
      const fenceLabel =
        geofences.find((g) => g.geofence_id === whereSelect.value)?.label
        ?? whereSelect.value;
      const who = joinNames(names);
      whoLegend.textContent =
        names.length === 0
          ? "When someone enters a geofence"
          : `When ${who} enter ${fenceLabel}`;
    };
    for (const p of users) {
      const row = document.createElement("label");
      row.className = "rules-check-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = p.user_id;
      cb.checked = selectedUsers.has(p.user_id);
      cb.addEventListener("change", () => {
        if (cb.checked) {
          selectedUsers.add(p.user_id);
        } else {
          selectedUsers.delete(p.user_id);
        }
        syncWhoLegend();
      });
      row.append(
        cb,
        document.createTextNode(
          ` ${userDisplayLabel(p.user_id, p.display_name)} (${p.tracking_device_label})`,
        ),
      );
      whoField.append(row);
    }

    const whereSelect = document.createElement("select");
    for (const g of geofences) {
      const opt = document.createElement("option");
      opt.value = g.geofence_id;
      opt.textContent = g.label;
      whereSelect.append(opt);
    }
    const existingGeofence =
      existing?.conditions.all.find((c) => c.type === RuleConditionType.UsersInsideGeofence)
        ?.geofence_id ?? "house";
    whereSelect.value = existingGeofence;
    whereSelect.addEventListener("change", () => {
      syncWhoLegend();
    });
    appendLabeledField(
      whoField,
      createFieldLabel("Geofence"),
      whereSelect,
    );
    syncWhoLegend();
    conditionsField.append(whoField);

    const existingDayCondition = existing?.conditions.all.find(
      (c): c is Extract<RuleConditionOut, { type: typeof RuleConditionType.DaysOfWeek }> =>
        c.type === RuleConditionType.DaysOfWeek,
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
      existing?.conditions.all.some((c) => c.type === RuleConditionType.AfterSunset) ?? true;
    sunsetRow.append(sunsetCb, document.createTextNode(" After sunset (sunset to midnight)"));
    timeField.append(sunsetRow);

    const sunriseRow = document.createElement("label");
    sunriseRow.className = "rules-check-row";
    const sunriseCb = document.createElement("input");
    sunriseCb.type = "checkbox";
    sunriseCb.checked =
      existing?.conditions.all.some((c) => c.type === RuleConditionType.BeforeSunrise) ?? false;
    sunriseRow.append(
      sunriseCb,
      document.createTextNode(" Before sunrise (midnight to sunrise)"),
    );
    timeField.append(sunriseRow);

    const existingWindow = existing?.conditions.all.find(
      (c): c is Extract<RuleConditionOut, { type: typeof RuleConditionType.LocalTimeWindow }> =>
        c.type === RuleConditionType.LocalTimeWindow,
    );
    const clockRow = document.createElement("div");
    clockRow.className = "settings-dialog-field-row rules-editor-clock-row";
    const clockStart = document.createElement("input");
    clockStart.type = "time";
    clockStart.value = hhmmToTimeInput(existingWindow?.start_hhmm ?? "");
    appendLabeledField(
      clockRow,
      createFieldLabel(
        "Clock window start (optional)",
        {
          detail: "Local start of a fixed daily window (paired with end below).",
          example: "22:00 for evening-only automations.",
        },
      ),
      clockStart,
    );

    const clockEnd = document.createElement("input");
    clockEnd.type = "time";
    clockEnd.value = hhmmToTimeInput(existingWindow?.end_hhmm ?? "");
    appendLabeledField(
      clockRow,
      createFieldLabel(
        "Clock window end (optional)",
        {
          detail: "Local end of the window. Both start and end are required to enable.",
          example: "06:00 for an overnight quiet-hours band.",
        },
      ),
      clockEnd,
    );
    timeField.append(clockRow);

    if (timeTemplates.length > 0) {
      const templatesLegend = document.createElement("p");
      templatesLegend.className = "rules-field-hint";
      templatesLegend.textContent = "Reusable templates from the Conditions tab:";
      timeField.append(templatesLegend);
      const selectedTemplateIds = new Set(
        existing?.conditions.all
          .filter(
            (c): c is Extract<RuleConditionOut, { type: typeof RuleConditionType.LocalTimeWindow }> =>
              c.type === RuleConditionType.LocalTimeWindow,
          )
          .map((c) =>
            timeTemplates.find(
              (t) => t.start_hhmm === c.start_hhmm && t.end_hhmm === c.end_hhmm,
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
        row.append(
          cb,
          document.createTextNode(
            ` ${template.label} (${template.start_hhmm}–${template.end_hhmm})`,
          ),
        );
        timeField.append(row);
      }
    }
    conditionsField.append(timeField);
    form.append(conditionsField);

    const tuningRow = document.createElement("div");
    tuningRow.className = "settings-dialog-field-row rules-editor-tuning-row";
    const accuracyInput = document.createElement("input");
    accuracyInput.type = "number";
    accuracyInput.min = "1";
    accuracyInput.step = "1";
    accuracyInput.value = String(existing?.min_location_accuracy_m ?? DEFAULT_MIN_LOCATION_ACCURACY_M);
    appendLabeledField(
      tuningRow,
      createFieldLabel("Min location accuracy (meters)", {
        detail:
          "Ignore GPS readings whose horizontal accuracy is worse than this threshold. Prevents a fuzzy phone reading from falsely placing someone inside a geofence.",
        example:
          "50 m drops a ±120 m reading in a parking lot while still accepting a ±12 m reading in the driveway.",
      }),
      accuracyInput,
    );

    const cooldownInput = document.createElement("input");
    cooldownInput.type = "number";
    cooldownInput.min = "0";
    cooldownInput.value = String(existing?.cooldown_s ?? 300);
    appendLabeledField(
      tuningRow,
      createFieldLabel("Cooldown (seconds)", {
        detail:
          "Minimum wait before the same rule can fire again after a successful run.",
        example:
          "300 prevents the garage from re-opening every poll when both people linger on the fence edge.",
      }),
      cooldownInput,
    );
    form.append(tuningRow);

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
    notifyEmail.multiple = true;
    notifyEmail.placeholder = "you@example.com, partner@example.com";
    notifyEmail.value = (existing?.notification_emails ?? []).join(", ");
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
      void (async () => {
        const userIds = [
          ...whoField.querySelectorAll<HTMLInputElement>("input:checked"),
        ].map((el) => el.value);
        if (userIds.length === 0) {
          showErrorToast("Select at least one user.");
          return;
        }
        const conditions: RuleOut["conditions"]["all"] = [
          {
            type: RuleConditionType.UsersInsideGeofence,
            geofence_id: whereSelect.value,
            user_ids: userIds,
          },
        ];
        if (sunsetCb.checked) {
          conditions.push({
            type: RuleConditionType.AfterSunset,
            offset_minutes: 0,
            window_end: AstronomicalWindowBoundary.Midnight,
          });
        }
        if (sunriseCb.checked) {
          conditions.push({
            type: RuleConditionType.BeforeSunrise,
            offset_minutes: 0,
            window_start: AstronomicalWindowBoundary.Midnight,
          });
        }
        if (clockStart.value !== "" && clockEnd.value !== "") {
          conditions.push({
            type: RuleConditionType.LocalTimeWindow,
            start_hhmm: clockStart.value,
            end_hhmm: clockEnd.value,
          });
        } else if (clockStart.value !== "" || clockEnd.value !== "") {
          showErrorToast("Clock window requires both start and end times.");
          return;
        }
        const selectedDays = dayPicker.getSelectedDays();
        if (selectedDays.length === 0) {
          showErrorToast("Select at least one day of the week.");
          return;
        }
        conditions.push({ type: RuleConditionType.DaysOfWeek, days: selectedDays });
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
          .map((row) => {
            const delayRaw = row.delayInput.value.trim();
            const delayParsed = delayRaw === "" ? 0 : Number(delayRaw);
            const entry: RuleDeviceActionOut = {
              family_id: row.device.family_id,
              device_id: row.device.device_id,
              display_name: row.device.label,
              action: row.actionSelect.value as RuleActionType,
            };
            if (Number.isFinite(delayParsed) && delayParsed > 0) {
              entry.delay_s = Math.floor(delayParsed);
            }
            return entry;
          });
        if (device_actions.length === 0) {
          showErrorToast("Select at least one device action.");
          return;
        }
        if (notifyCb.checked && notifyEmail.value.trim() === "") {
          showErrorToast("Enter a notification email or disable email notification.");
          return;
        }
        const ruleId = slugifyId(idInput.value.trim());
        if (ruleId === "") {
          showErrorToast("Rule id is required.");
          return;
        }
        const allRules = await this.dataSource.listRules();
        const duplicate = allRules.find(
          (candidate) => candidate.id === ruleId && candidate.id !== existing?.id,
        );
        if (duplicate !== undefined) {
          showErrorToast(
            `Rule id "${ruleId}" is already used by "${duplicate.label}".`,
          );
          return;
        }
        const rule: RuleOut = {
          id: ruleId,
          label: labelInput.value.trim(),
          enabled: ruleEnabled,
          triggers: [RuleTrigger.EdgeTrue],
          schedule_cron: null,
          cooldown_s: Number(cooldownInput.value) || 300,
          min_location_accuracy_m: Number(accuracyInput.value) || DEFAULT_MIN_LOCATION_ACCURACY_M,
          notify_on_fire: notifyCb.checked,
          notification_emails: notifyCb.checked
            ? notifyEmail.value
                .split(",")
                .map((entry) => entry.trim())
                .filter((entry) => entry.length > 0)
            : [],
          conditions: { all: conditions },
          device_actions,
        };
        await this.dataSource.saveRule(rule);
        editor.remove();
        void this.refresh();
      })();
    });

    editor.append(form);
    this.body.append(editor);
    labelInput.focus();
  }

  private async openRuleInspector(
    rule: RuleOut,
    liveStatus?: RuleStatusSummaryOut,
  ): Promise<void> {
    const [users, geofences, actionDevices] = await Promise.all([
      this.dataSource.listUsers(),
      this.dataSource.listGeofences(),
      this.dataSource.listActionDevices(),
    ]);
    for (const panel of this.body.querySelectorAll(".rules-inspector-panel")) {
      panel.remove();
    }
    const panel = document.createElement("div");
    panel.className = "rules-editor-panel rules-inspector-panel";
    panel.dataset.ruleId = rule.id;
    const resolvedLiveStatus = liveStatus ?? this.liveStatusForRule(rule.id);
    const inspectorOptions: RuleInspectorMountOptions = {
      onClose: () => {
        panel.remove();
      },
    };
    if (resolvedLiveStatus !== undefined) {
      inspectorOptions.liveStatus = resolvedLiveStatus;
    }
    mountRuleInspectorPanel(
      panel,
      rule,
      buildInspectorContext(users, geofences, actionDevices),
      inspectorOptions,
    );
    this.body.append(panel);
    panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  private async refresh(): Promise<void> {
    this.status = await this.dataSource.getStatus();
    this.syncSourcePill();
    await this.renderBody();
    this.syncTabUi();
  }

  private syncSourcePill(): void {
    if (this.dataSource.isRulesFileBacked()) {
      if (this.sourcePill === null) {
        this.sourcePill = document.createElement("span");
        this.sourcePill.className = "rules-source-pill";
        this.sourcePill.textContent = "Rules: automation-rules.json";
        this.titleWrap.append(this.sourcePill);
      }
      return;
    }
    if (this.sourcePill !== null) {
      this.sourcePill.remove();
      this.sourcePill = null;
    }
  }

  private async refreshPresenceMap(): Promise<void> {
    if (this.presenceMap === null) {
      return;
    }
    try {
      const users = await this.dataSource.listUserStatus();
      this.presenceMap.updateUsers(
        users.map(userStatusToMapUser),
      );
    } catch (err) {
      console.warn("User presence map refresh failed", err);
    }
  }

  private clearHomeLocationMap(): void {
    if (this.homeLocationMapDebounceTimer !== null) {
      window.clearTimeout(this.homeLocationMapDebounceTimer);
      this.homeLocationMapDebounceTimer = null;
    }
    this.homeLocationMapGeneration += 1;
    this.homeLocationMap?.destroy();
    this.homeLocationMap = null;
  }

  private async renderBody(): Promise<void> {
    this.stopPresencePoll();
    this.clearHomeLocationMap();
    this.presenceMap?.destroy();
    this.presenceMap = null;
    this.body.replaceChildren();
    if (this.status === null) {
      return;
    }
    switch (this.activeTab) {
      case RulesTabId.Status:
        await this.renderStatusTab(this.status);
        break;
      case RulesTabId.Conditions:
        await this.renderConditionsTab(this.status);
        break;
      case RulesTabId.Rules:
        await this.renderRulesTab();
        break;
      case RulesTabId.Geofences:
        await this.renderGeofencesTab();
        break;
      case RulesTabId.Mail:
        await this.renderMailTab();
        break;
      case RulesTabId.Users:
        await this.renderUsersTab();
        break;
      case RulesTabId.Vacation:
        await this.renderVacationTab();
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

  private async renderVacationTab(): Promise<void> {
    const mount = document.createElement("div");
    mount.className = "rules-vacation-mount";
    this.body.append(mount);
    const { mountVacationModeSettingsPanel } = await import(
      "./vacation-mode-settings-panel.js"
    );
    await mountVacationModeSettingsPanel(mount, this.dataSource);
  }

  private async renderConditionsTab(status: RulesStatusOut): Promise<void> {
    const templates = await this.dataSource.listTimeConditionTemplates();
    const settings = await this.dataSource.getSettingsLocation();
    const homeGeofence = resolveHomeGeofence(status.geofences, settings);
    const sunriseMsg = beforeSunriseStatusMessage(status.sun);
    const daylightMsg = daylightStatusMessage(status.sun);
    const sunsetMsg = afterSunsetStatusMessage(status.sun);
    const openHomeGeofence = (geofenceId: string | null): void => {
      this.pendingGeofenceFocusId = geofenceId;
      void this.setTab(RulesTabId.Geofences);
    };

    const dynamicHeading = document.createElement("h3");
    dynamicHeading.className = "rules-section-title";
    dynamicHeading.textContent = "Astronomical (dynamic)";

    const sunriseCard = appendAstronomicalConditionCard(
      sunriseMsg.dynamicLabel,
      sunriseMsg.primary,
      "Before sunrise",
      BEFORE_SUNRISE_WINDOW_DESCRIPTION,
      homeGeofence,
      settings,
      openHomeGeofence,
    );
    const daylightCard = appendAstronomicalConditionCard(
      daylightMsg.dynamicLabel,
      daylightMsg.primary,
      "Daylight",
      DAYLIGHT_WINDOW_DESCRIPTION,
      homeGeofence,
      settings,
      openHomeGeofence,
    );
    const sunsetCard = appendAstronomicalConditionCard(
      sunsetMsg.dynamicLabel,
      sunsetMsg.primary,
      "After sunset",
      AFTER_SUNSET_WINDOW_DESCRIPTION,
      homeGeofence,
      settings,
      openHomeGeofence,
    );

    const clockHeading = document.createElement("h3");
    clockHeading.className = "rules-section-title";
    clockHeading.textContent = "Clock-time templates";
    const clockLead = document.createElement("p");
    clockLead.className = "settings-dialog-lead";
    clockLead.textContent =
      "Fixed local-time windows complement sunset/sunrise. Each template has a start and end time.";

    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const template of templates) {
      const card = document.createElement("article");
      card.className = "rules-card";
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const title = document.createElement("strong");
      title.textContent = `${template.label} (${template.start_hhmm}–${template.end_hhmm})`;
      const delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "btn btn-danger";
      delBtn.textContent = "Delete";
      delBtn.addEventListener("click", () => {
        void confirmAction({
          message: `Delete template "${template.label}"?`,
          confirmLabel: "Delete",
          variant: ConfirmButtonVariant.Danger,
        }).then((confirmed) => {
          if (!confirmed) {
            return;
          }
          void this.dataSource
            .deleteTimeConditionTemplate(template.template_id)
            .then(() => this.refresh());
        });
      });
      row.append(title, delBtn);
      card.append(row);
      list.append(card);
    }

    const form = document.createElement("form");
    form.className = "rules-user-form";
    const labelInput = document.createElement("input");
    labelInput.required = true;
    labelInput.placeholder = "Weekend late night";
    appendLabeledField(form, createFieldLabel("Template label"), labelInput);
    const startInput = document.createElement("input");
    startInput.type = "time";
    startInput.required = true;
    appendLabeledField(form, createFieldLabel("Start time"), startInput);
    const endInput = document.createElement("input");
    endInput.type = "time";
    endInput.required = true;
    appendLabeledField(form, createFieldLabel("End time"), endInput);
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
        start_hhmm: startInput.value,
        end_hhmm: endInput.value,
      };
      void this.dataSource.saveTimeConditionTemplate(template).then(() => {
        form.reset();
        void this.refresh();
      });
    });

    this.body.append(
      dynamicHeading,
      sunriseCard,
      daylightCard,
      sunsetCard,
      clockHeading,
      clockLead,
      list,
      form,
    );
  }

  private async renderStatusTab(status: RulesStatusOut): Promise<void> {
    const sunMsg = afterSunsetStatusMessage(status.sun);
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
      void this.setTab(RulesTabId.Conditions);
    });

    const usersHeading = document.createElement("h3");
    usersHeading.className = "rules-section-title";
    usersHeading.textContent = "Users";
    const usersSection = document.createElement("section");
    usersSection.className = "rules-users-section";

    const rulesHeading = document.createElement("h3");
    rulesHeading.className = "rules-section-title";
    rulesHeading.textContent = "Rules";
    const rulesReadOnly = this.dataSource.isRulesFileBacked();
    const ruleList = document.createElement("div");
    ruleList.className = "rules-card-list";
    for (const rule of status.rules) {
      const card = document.createElement("article");
      card.className = "rules-card rules-status-rule-card";
      card.dataset.ruleId = rule.id;
      const row = document.createElement("div");
      row.className = "rules-card-row";
      const nameBtn = document.createElement("button");
      nameBtn.type = "button";
      nameBtn.className = "rules-card-title-btn";
      nameBtn.textContent = rule.label;
      nameBtn.addEventListener("click", () => {
        void this.navigateToRule(rule.id);
      });
      row.append(nameBtn);
      if (rule.reference_issues.length > 0) {
        row.append(createBrokenRuleBadge(rule.reference_issues));
      } else if (!rulesReadOnly) {
        const enableToggle = createEnableToggle(rule.enabled, (next) => {
          void this.dataSource
            .setRuleEnabled(rule.id, next)
            .then(() => this.refresh());
        });
        row.append(enableToggle);
      }
      const meta = document.createElement("p");
      meta.className = "rules-card-meta";
      meta.textContent = ruleStatusHeadline(rule);
      card.append(row, meta);
      appendRuleLastMetLine(card, rule);
      if (rule.last_error !== null) {
        const error = document.createElement("p");
        error.className = "rules-card-warn";
        error.textContent = rule.last_error;
        card.append(error);
      }
      void this.dataSource.getRule(rule.id).then((definition) => {
        if (definition === null) {
          return;
        }
        const userIds = collectUserIdsFromRule(definition);
        if (userIds.length === 0) {
          return;
        }
        const presence = document.createElement("div");
        presence.className = "rules-rule-presence-summary";
        for (const userId of userIds) {
          const userRow = resolveRosterUser(userId, status.users);
          const line = document.createElement("p");
          line.className = "rules-card-meta";
          if (userRow === undefined) {
            line.textContent =
              `"${userId}": not in user roster (sync users from My Tracks)`;
            presence.append(line);
            continue;
          }
          const name = userDisplayLabel(userRow.user_id, userRow.display_name);
          const where = userRow.last_location === null
            ? "No location yet"
            : formatInsideGeofencesLine(
              userRow.inside_geofence_ids,
              status.geofences,
            );
          line.textContent = `${name}: ${where}`;
          presence.append(line);
        }
        card.append(presence);
      });
      ruleList.append(card);
    }

    this.body.append(sunBtn, usersHeading, usersSection, rulesHeading, ruleList);
    this.mountUserPresenceMap(
      usersSection,
      status.geofences,
      status.users,
      { showUserFilters: true, showTextDetails: false },
    );
    const focusRuleId = this.pendingStatusRuleInspectorId;
    if (focusRuleId !== null) {
      this.pendingStatusRuleInspectorId = null;
      const statusRule = status.rules.find((row) => row.id === focusRuleId);
      const card = ruleList.querySelector<HTMLElement>(
        `[data-rule-id="${CSS.escape(focusRuleId)}"]`,
      );
      if (card !== null) {
        card.tabIndex = -1;
        card.classList.add("rules-status-rule-card-focused");
        card.focus({ preventScroll: true });
        card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
      if (statusRule !== undefined) {
        const definition = await this.dataSource.getRule(focusRuleId);
        if (this.activeTab !== RulesTabId.Status || !this.body.contains(ruleList)) {
          return;
        }
        if (definition !== null) {
          await this.openRuleInspector(definition, statusRule);
        }
      }
    }
  }

  private async renderRulesTab(): Promise<void> {
    const rulesReadOnly = this.dataSource.isRulesFileBacked();
    const [rules, users, geofences, actionDevices] = await Promise.all([
      this.dataSource.listRules(),
      this.dataSource.listUsers(),
      this.dataSource.listGeofences(),
      this.dataSource.listActionDevices(),
    ]);
    const summaryContext = buildRuleSummaryContext(
      users,
      geofences,
      actionDevices,
    );
    if (rulesReadOnly) {
      const hint = document.createElement("p");
      hint.className = "rules-card-meta";
      hint.textContent =
        "Rules are loaded from automation-rules.json on the server. Edit that file and restart to change them.";
      this.body.append(hint);
    } else {
      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "btn";
      addBtn.textContent = "Add rule";
      addBtn.addEventListener("click", () => {
        void this.openRuleEditor(null);
      });
      this.body.append(addBtn);
    }
    const list = document.createElement("div");
    list.className = "rules-card-list";
    for (const rule of rules) {
      const card = document.createElement("article");
      card.className = "rules-card";
      card.classList.toggle("rules-card-disabled", !rule.enabled);

      const top = document.createElement("div");
      top.className = "rules-rule-card-top";
      if (rulesReadOnly) {
        const nameBtn = document.createElement("button");
        nameBtn.type = "button";
        nameBtn.className = "rules-card-title-btn rules-rule-card-title";
        nameBtn.textContent = rule.label;
        nameBtn.addEventListener("click", () => {
          void this.openRuleInspector(rule, this.liveStatusForRule(rule.id));
        });
        top.append(nameBtn);
      } else {
        const title = document.createElement("h3");
        title.className = "rules-rule-card-title";
        title.textContent = rule.label;
        const enableToggle = createEnableToggle(rule.enabled, (next) => {
          void this.dataSource
            .setRuleEnabled(rule.id, next)
            .then(() => this.refresh());
        });
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
          void confirmAction({
            message: `Delete rule "${rule.label}"?`,
            confirmLabel: "Delete",
            variant: ConfirmButtonVariant.Danger,
          }).then((confirmed) => {
            if (!confirmed) {
              return;
            }
            void this.dataSource.deleteRule(rule.id).then(() => this.refresh());
          });
        });
        actions.append(editBtn, delBtn);
        top.append(enableToggle, title, actions);
      }
      card.append(top);

      appendRuleSummaryBody(card, summarizeRule(rule, summaryContext));

      if (rule.notify_on_fire && rule.notification_emails.length > 0) {
        const notifyMeta = document.createElement("p");
        notifyMeta.className = "rules-card-meta";
        notifyMeta.textContent = `Email on fire → ${rule.notification_emails.join(", ")}`;
        card.append(notifyMeta);
      }

      list.append(card);
    }
    this.body.append(list);
    const focusRuleId = this.pendingRuleInspectorId;
    if (focusRuleId !== null) {
      this.pendingRuleInspectorId = null;
      const rule = rules.find((row) => row.id === focusRuleId);
      if (rule !== undefined) {
        await this.openRuleInspector(rule, this.liveStatusForRule(rule.id));
      }
    }
  }

  private async renderGeofencesTab(): Promise<void> {
    const mount = document.createElement("div");
    mount.className = "rules-geofence-mount";
    mount.dataset.testid = "rules-geofence-mount";
    this.body.append(mount);
    const { mountGeofenceMapPanel } = await import("./geofence-map.js");
    await mountGeofenceMapPanel(mount, this.dataSource, () => this.refresh());
    const focusId = this.pendingGeofenceFocusId;
    if (focusId !== null) {
      this.pendingGeofenceFocusId = null;
      const row = mount.querySelector<HTMLTableRowElement>(
        `tr[data-geofence-id="${CSS.escape(focusId)}"]`,
      );
      if (row !== null) {
        row.classList.add("rules-geofence-row-focused");
        row.scrollIntoView({ block: "nearest", behavior: "smooth" });
      }
    }
  }

  private async renderUsersTab(): Promise<void> {
    const geofences = await this.dataSource.listGeofences();
    const sync = await this.dataSource.getMyTracksUsersSync();
    const settings = await this.dataSource.getMyTracksSettings();
    const mapUsers = await this.dataSource.listUserStatus();
    const rosterUsers = await this.dataSource.listUsers();
    const homeLocation = await this.dataSource.getSettingsLocation();

    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead";
    appendMyTracksInstanceText(lead, {
      before: "Users sync from ",
      domain: settings?.domain ?? "",
      after:
        ". Presence updates arrive via webhook; edit people there, then sync here.",
    });

    const syncRow = document.createElement("div");
    syncRow.className = "rules-users-sync";
    const syncMeta = document.createElement("p");
    syncMeta.className = "rules-card-meta";
    syncMeta.replaceChildren(
      document.createTextNode(`${sync.user_count} users · last synced `),
    );
    if (sync.last_synced_at === null) {
      syncMeta.append(document.createTextNode("never"));
    } else {
      syncMeta.append(createAuditedTimeElement(sync.last_synced_at));
    }
    const syncBtn = document.createElement("button");
    syncBtn.type = "button";
    syncBtn.className = "btn btn-secondary";
    syncBtn.textContent = "Sync from My Tracks";
    syncBtn.addEventListener("click", () => {
      void runMyTracksSyncAction(this.dataSource, MyTracksSyncKind.Users, () => this.refresh());
    });
    syncRow.append(syncMeta, syncBtn);

    const main = document.createElement("div");
    main.className = "rules-users-main-layout";
    const left = document.createElement("div");
    left.className = "rules-users-main-left";
    const mapsStack = document.createElement("div");
    mapsStack.className = "rules-users-maps-stack";

    const homeMapBlock = document.createElement("div");
    homeMapBlock.className = "rules-users-map-block";
    const homeMapTitle = document.createElement("h4");
    homeMapTitle.className = "rules-users-map-title";
    homeMapTitle.textContent = "Home location";
    const homeMapEl = document.createElement("div");
    homeMapEl.className = "rules-home-location-map";
    homeMapEl.setAttribute("role", "img");
    homeMapEl.setAttribute("aria-label", "Home location map");
    homeMapBlock.append(homeMapTitle, homeMapEl);

    const presenceMapBlock = document.createElement("div");
    presenceMapBlock.className = "rules-users-map-block";
    const presenceMapTitle = document.createElement("h4");
    presenceMapTitle.className = "rules-users-map-title";
    presenceMapTitle.textContent = "Live locations";
    const presenceMount = document.createElement("div");
    presenceMount.className = "rules-users-presence-map-inset";
    presenceMapBlock.append(presenceMapTitle, presenceMount);
    mapsStack.append(homeMapBlock, presenceMapBlock);

    const homeBlock = this.mountHomeLocationForm(
      left,
      homeLocation,
      mapUsers,
      homeMapEl,
    );
    await this.mountHouseholdAndSharedWifi(left, rosterUsers, homeBlock.sharedWifiSelect);

    main.append(left, mapsStack);
    this.body.append(lead, syncRow, main);

    this.mountUserPresenceMap(
      presenceMount,
      geofences,
      mapUsers,
      {
        compact: true,
        includeUserIdInTooltip: true,
        showLegend: false,
        showUserFilters: false,
        showTextDetails: false,
      },
    );
    // Maps were created before the grid had a real width; re-measure now.
    this.homeLocationMap?.invalidateSize();
    this.presenceMap?.invalidateSize();
    requestAnimationFrame(() => {
      this.homeLocationMap?.invalidateSize();
      this.presenceMap?.invalidateSize();
    });
  }

  private mountHomeLocationForm(
    parent: HTMLElement,
    initial: SettingsLocationOut,
    mapUsers: UserStatusOut[],
    mapEl: HTMLElement,
  ): { sharedWifiSelect: HTMLSelectElement } {
    const section = document.createElement("section");
    section.className = "rules-users-section rules-home-location-section";
    const heading = document.createElement("h3");
    heading.className = "rules-section-title";
    heading.textContent = "Home location";
    const status = document.createElement("p");
    status.className = "rules-card-meta rules-home-location-status";

    const form = document.createElement("form");
    form.className = "rules-home-location-form";
    form.setAttribute("autocomplete", "off");

    const labelTzRow = document.createElement("div");
    labelTzRow.className = "settings-dialog-field-row rules-home-location-fields";
    const labelInput = document.createElement("input");
    labelInput.type = "text";
    labelInput.value = initial.home_label ?? "";
    preventBrowserAutofill(labelInput);
    appendLabeledField(labelTzRow, createFieldLabel("Label"), labelInput);
    const tzInput = document.createElement("input");
    tzInput.type = "text";
    tzInput.required = true;
    tzInput.value = initial.timezone;
    preventBrowserAutofill(tzInput);
    appendLabeledField(labelTzRow, createFieldLabel("Timezone (IANA)"), tzInput);
    form.append(labelTzRow);

    const latLonRow = document.createElement("div");
    latLonRow.className = "settings-dialog-field-row rules-home-location-fields";
    const latInput = document.createElement("input");
    latInput.type = "number";
    latInput.step = "any";
    latInput.min = "-90";
    latInput.max = "90";
    latInput.required = true;
    latInput.value = String(initial.lat);
    preventBrowserAutofill(latInput);
    appendLabeledField(latLonRow, createFieldLabel("Latitude"), latInput);
    const lonInput = document.createElement("input");
    lonInput.type = "number";
    lonInput.step = "any";
    lonInput.min = "-180";
    lonInput.max = "180";
    lonInput.required = true;
    lonInput.value = String(initial.lon);
    preventBrowserAutofill(lonInput);
    appendLabeledField(latLonRow, createFieldLabel("Longitude"), lonInput);
    form.append(latLonRow);

    const actions = document.createElement("div");
    actions.className = "rules-home-location-actions";
    const browserBtn = document.createElement("button");
    browserBtn.type = "button";
    browserBtn.className = "btn btn-secondary";
    browserBtn.textContent = "Adopt browser location";
    const adoptSelect = document.createElement("select");
    adoptSelect.className = "rules-select";
    adoptSelect.setAttribute("aria-label", "Adopt home location from user");
    const adoptPlaceholder = document.createElement("option");
    adoptPlaceholder.value = "";
    adoptPlaceholder.textContent = "Adopt from user…";
    adoptSelect.append(adoptPlaceholder);
    for (const user of mapUsers) {
      if (user.last_location === null) {
        continue;
      }
      const option = document.createElement("option");
      option.value = user.user_id;
      option.textContent = userDisplayLabel(user.user_id, user.display_name);
      adoptSelect.append(option);
    }
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Save home location";
    actions.append(browserBtn, adoptSelect, saveBtn);
    form.append(actions);

    this.homeLocationMap?.destroy();
    this.homeLocationMap = mountHomeLocationMapInset(mapEl, {
      label: initial.home_label,
      lat: initial.lat,
      lon: initial.lon,
    });
    const mapGeneration = this.homeLocationMapGeneration;

    const syncMapFromInputs = (): void => {
      if (mapGeneration !== this.homeLocationMapGeneration) {
        return;
      }
      const lat = Number(latInput.value);
      const lon = Number(lonInput.value);
      const labelRaw = labelInput.value.trim();
      this.homeLocationMap?.setLocation(
        lat,
        lon,
        labelRaw === "" ? null : labelRaw,
      );
    };
    const scheduleMapSync = (): void => {
      if (this.homeLocationMapDebounceTimer !== null) {
        window.clearTimeout(this.homeLocationMapDebounceTimer);
      }
      const scheduledGeneration = this.homeLocationMapGeneration;
      this.homeLocationMapDebounceTimer = window.setTimeout(() => {
        this.homeLocationMapDebounceTimer = null;
        if (scheduledGeneration !== this.homeLocationMapGeneration) {
          return;
        }
        syncMapFromInputs();
      }, 200);
    };

    const refreshStatus = (location: SettingsLocationOut): void => {
      status.textContent = location.home_configured
        ? `Configured · ${location.lat.toFixed(5)}, ${location.lon.toFixed(5)}`
        : "Not configured (0, 0 sentinel)";
    };
    refreshStatus(initial);

    latInput.addEventListener("input", scheduleMapSync);
    lonInput.addEventListener("input", scheduleMapSync);
    labelInput.addEventListener("input", scheduleMapSync);

    browserBtn.addEventListener("click", () => {
      if (!("geolocation" in navigator)) {
        showErrorToast("Browser geolocation is not available in this environment.");
        return;
      }
      browserBtn.disabled = true;
      navigator.geolocation.getCurrentPosition(
        (position) => {
          latInput.value = String(position.coords.latitude);
          lonInput.value = String(position.coords.longitude);
          browserBtn.disabled = false;
          syncMapFromInputs();
        },
        (err) => {
          browserBtn.disabled = false;
          showErrorToast(
            `Could not read browser location: ${err.message || "permission denied"}`,
          );
        },
        { enableHighAccuracy: true, timeout: 15_000 },
      );
    });

    adoptSelect.addEventListener("change", () => {
      const userId = adoptSelect.value;
      if (userId === "") {
        return;
      }
      const user = mapUsers.find((row) => row.user_id === userId);
      const location = user?.last_location ?? null;
      if (location === null) {
        showErrorToast(`Expected a last location for user ${userId}, got none`);
        adoptSelect.value = "";
        return;
      }
      latInput.value = String(location.lat);
      lonInput.value = String(location.lon);
      adoptSelect.value = "";
      syncMapFromInputs();
    });

    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const lat = Number(latInput.value);
      const lon = Number(lonInput.value);
      if (!Number.isFinite(lat) || !Number.isFinite(lon)) {
        showErrorToast("Expected finite latitude and longitude.");
        return;
      }
      const timezone = tzInput.value.trim();
      if (timezone === "") {
        showErrorToast("Expected a non-empty IANA timezone.");
        return;
      }
      const labelRaw = labelInput.value.trim();
      saveBtn.disabled = true;
      void this.dataSource
        .saveSettingsLocation({
          home_configured: !(lat === 0 && lon === 0),
          home_label: labelRaw === "" ? null : labelRaw,
          lat,
          lon,
          timezone,
          wifi_home_geofence_id: initial.wifi_home_geofence_id ?? null,
          wifi_home_presence_enabled: initial.wifi_home_presence_enabled ?? true,
        })
        .then((saved) => {
          refreshStatus(saved);
          this.homeLocationMap?.setLocation(
            saved.lat,
            saved.lon,
            saved.home_label,
          );
          void this.refresh();
        })
        .catch((err: unknown) => {
          const detail = err instanceof Error ? err.message : String(err);
          showErrorToast(`Failed to save home location: ${detail}`);
        })
        .finally(() => {
          saveBtn.disabled = false;
        });
    });

    const sharedWifiSelect = document.createElement("select");
    sharedWifiSelect.className = "rules-select rules-users-shared-wifi-select";
    sharedWifiSelect.setAttribute("aria-label", "Home WiFi for household");

    section.append(heading, status, form);
    parent.append(section);
    return { sharedWifiSelect };
  }

  private async mountHouseholdAndSharedWifi(
    parent: HTMLElement,
    rosterUsers: UserOut[],
    sharedWifiSelect: HTMLSelectElement,
  ): Promise<void> {
    const householdSection = document.createElement("section");
    householdSection.className = "rules-users-section";
    const householdHeading = document.createElement("h3");
    householdHeading.className = "rules-section-title";
    householdHeading.textContent = "Household";
    const householdLead = document.createElement("p");
    householdLead.className = "rules-card-meta";
    householdLead.textContent =
      "People who share this home WiFi. Colored dots match Live locations.";
    const householdList = document.createElement("div");
    householdList.className = "rules-users-household-list";

    const wifiSection = document.createElement("section");
    wifiSection.className = "rules-users-section";
    const wifiHeading = document.createElement("h3");
    wifiHeading.className = "rules-section-title";
    wifiHeading.textContent = "Home WiFi";
    const wifiLead = document.createElement("p");
    wifiLead.className = "rules-card-meta";
    wifiLead.textContent = "One SSID for all household members (matched by BSSID).";

    const noneOption = document.createElement("option");
    noneOption.value = "";
    noneOption.textContent = "Not set";
    sharedWifiSelect.append(noneOption);

    const householdMembers = rosterUsers.filter((user) => user.is_household);
    const networkByBssid = new Map<string, { wifi_bssid: string; wifi_ssid: string }>();
    for (const user of householdMembers) {
      try {
        const networks = await this.dataSource.listUserObservedWifi(user.user_id);
        for (const network of networks) {
          networkByBssid.set(network.wifi_bssid, {
            wifi_bssid: network.wifi_bssid,
            wifi_ssid: network.wifi_ssid,
          });
        }
      } catch (err) {
        console.warn("Failed to load observed WiFi networks", err);
      }
    }
    let sharedBssid: string | null = null;
    let sharedSsid: string | null = null;
    for (const user of householdMembers) {
      if (user.home_wifi_bssid !== null) {
        sharedBssid = user.home_wifi_bssid;
        sharedSsid = user.home_wifi_ssid;
        break;
      }
    }
    if (sharedBssid !== null && sharedSsid !== null && !networkByBssid.has(sharedBssid)) {
      networkByBssid.set(sharedBssid, {
        wifi_bssid: sharedBssid,
        wifi_ssid: sharedSsid,
      });
    }
    for (const network of [...networkByBssid.values()].sort((a, b) =>
      a.wifi_ssid.localeCompare(b.wifi_ssid),
    )) {
      const option = document.createElement("option");
      option.value = network.wifi_bssid;
      option.textContent = network.wifi_ssid;
      option.dataset.ssid = network.wifi_ssid;
      sharedWifiSelect.append(option);
    }
    if (sharedBssid !== null) {
      sharedWifiSelect.value = sharedBssid;
    }

    sharedWifiSelect.addEventListener("change", () => {
      sharedWifiSelect.disabled = true;
      void this.applySharedHomeWifiToHousehold(rosterUsers, sharedWifiSelect).finally(() => {
        sharedWifiSelect.disabled = false;
      });
    });

    for (const user of rosterUsers) {
      const row = document.createElement("label");
      row.className = "rules-check-row rules-users-household-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = user.is_household;
      cb.addEventListener("change", () => {
        cb.disabled = true;
        void this.toggleHouseholdMembership(
          user,
          cb.checked,
          sharedWifiSelect,
        )
          .catch(() => {
            // Resync from server so checkbox / wifi stay consistent after a partial failure.
            void this.refresh().catch((refreshErr: unknown) => {
              console.warn("Failed to refresh Users tab after household error", refreshErr);
            });
          })
          .finally(() => {
            cb.disabled = false;
          });
      });
      const swatch = document.createElement("span");
      swatch.className = "rules-users-household-swatch";
      swatch.style.backgroundColor = userMarkerColor(
        user.tracking_device_label,
        user.user_id,
      );
      swatch.setAttribute("aria-hidden", "true");
      swatch.title = "Live location marker color";
      row.append(
        cb,
        swatch,
        document.createTextNode(
          ` ${userDisplayLabel(user.user_id, user.display_name)}`,
        ),
      );
      householdList.append(row);
    }

    householdSection.append(householdHeading, householdLead, householdList);
    wifiSection.append(wifiHeading, wifiLead, sharedWifiSelect);
    parent.append(householdSection, wifiSection);
  }

  private async applySharedHomeWifiToHousehold(
    rosterUsers: UserOut[],
    select: HTMLSelectElement,
  ): Promise<void> {
    const selected = select.options[select.selectedIndex];
    const wifiBssid = select.value === "" ? null : select.value;
    const wifiSsid =
      wifiBssid === null
        ? null
        : selected?.dataset.ssid ?? selected?.textContent ?? null;
    const household = rosterUsers.filter((user) => user.is_household);
    const failedLabels: string[] = [];
    for (const user of household) {
      try {
        await this.dataSource.setUserHomeWifi(user.user_id, {
          wifi_bssid: wifiBssid,
          wifi_ssid: wifiSsid,
        });
        user.home_wifi_bssid = wifiBssid;
        user.home_wifi_ssid = wifiSsid;
      } catch (err) {
        const detail =
          err instanceof Error ? err.message : "Failed to save home WiFi";
        failedLabels.push(`${user.display_name}: ${detail}`);
      }
    }
    if (failedLabels.length > 0) {
      showErrorToast(
        `Home WiFi not saved for ${failedLabels.length} of ${household.length} household member(s): ${failedLabels.join("; ")}`,
      );
    }
  }

  private async mergeObservedWifiIntoSelect(
    userId: string,
    select: HTMLSelectElement,
  ): Promise<void> {
    let networks: { wifi_bssid: string; wifi_ssid: string }[] = [];
    try {
      networks = await this.dataSource.listUserObservedWifi(userId);
    } catch (err) {
      console.warn("Failed to load observed WiFi networks", err);
      return;
    }
    const existing = new Set(
      [...select.options].map((option) => option.value).filter((value) => value !== ""),
    );
    for (const network of networks) {
      if (existing.has(network.wifi_bssid)) {
        continue;
      }
      existing.add(network.wifi_bssid);
      const option = document.createElement("option");
      option.value = network.wifi_bssid;
      option.textContent = network.wifi_ssid;
      option.dataset.ssid = network.wifi_ssid;
      select.append(option);
    }
  }

  private async toggleHouseholdMembership(
    user: UserOut,
    isHousehold: boolean,
    sharedWifiSelect: HTMLSelectElement,
  ): Promise<void> {
    try {
      const saved = await this.dataSource.setUserHousehold(user.user_id, {
        is_household: isHousehold,
      });
      user.is_household = saved.is_household;
      if (isHousehold) {
        const selected = sharedWifiSelect.options[sharedWifiSelect.selectedIndex];
        const wifiBssid =
          sharedWifiSelect.value === "" ? null : sharedWifiSelect.value;
        const wifiSsid =
          wifiBssid === null
            ? null
            : selected?.dataset.ssid ?? selected?.textContent ?? null;
        if (wifiBssid !== null) {
          const withWifi = await this.dataSource.setUserHomeWifi(user.user_id, {
            wifi_bssid: wifiBssid,
            wifi_ssid: wifiSsid,
          });
          user.home_wifi_bssid = withWifi.home_wifi_bssid;
          user.home_wifi_ssid = withWifi.home_wifi_ssid;
        }
        await this.mergeObservedWifiIntoSelect(user.user_id, sharedWifiSelect);
      } else {
        const cleared = await this.dataSource.setUserHomeWifi(user.user_id, {
          wifi_bssid: null,
          wifi_ssid: null,
        });
        user.home_wifi_bssid = cleared.home_wifi_bssid;
        user.home_wifi_ssid = cleared.home_wifi_ssid;
      }
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "Failed to update household";
      showErrorToast(message);
      throw err;
    }
  }

  private async setTab(tab: RulesTabId): Promise<void> {
    this.activeTab = tab;
    await this.refresh();
  }

  private startPresencePoll(): void {
    this.stopPresencePoll();
    this.presencePollTimer = window.setInterval(() => {
      void this.refreshPresenceMap();
    }, RulesHubController.PRESENCE_POLL_MS);
  }

  private stopPresencePoll(): void {
    if (this.presencePollTimer !== null) {
      window.clearInterval(this.presencePollTimer);
      this.presencePollTimer = null;
    }
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

export async function openAutomationsHubDialog(
  options: AutomationsHubOpenOptions = {},
): Promise<void> {
  const dataSource = await createRulesDataSource();
  const hub = new RulesHubController(dataSource, options);
  await hub.open();
}

/** @deprecated Use openAutomationsHubDialog */
export async function openRulesHubDialog(): Promise<void> {
  await openAutomationsHubDialog();
}

export function parseAutomationsDeepLink(
  hash: string,
): AutomationsHubOpenOptions | null {
  const raw = hash.startsWith("#") ? hash.slice(1) : hash;
  const statusMatch = /^\/automations\/status\/([^/?#]+)(?:[?#].*)?$/.exec(raw);
  if (statusMatch !== null) {
    try {
      const ruleId = decodeURIComponent(statusMatch[1] ?? "");
      if (ruleId === "") {
        return null;
      }
      return { tab: RulesTabId.Status, ruleId };
    } catch {
      return null;
    }
  }
  const tabMatch = /^\/automations\/(mail|vacation)(?:[/?#].*)?$/.exec(raw);
  if (tabMatch === null) {
    return null;
  }
  const tab = tabMatch[1];
  if (tab === RulesTabId.Mail || tab === RulesTabId.Vacation) {
    return { tab };
  }
  return null;
}
