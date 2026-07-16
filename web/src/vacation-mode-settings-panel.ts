// Vacation mode settings panel (Automations hub).

import { HttpError } from "./api.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel } from "./rules-ui-helpers.js";
import type {
  UserOut,
  VacationModeSettingsOut,
  VacationModeSettingsStatusOut,
} from "./types.js";
import { showSuccessToast } from "./ui-toast.js";

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

function parseEmailList(raw: string): string[] {
  return raw
    .split(",")
    .map((part) => part.trim())
    .filter((part) => part !== "");
}

function settingsFromForm(
  enabled: boolean,
  userIds: string[],
  minDistanceM: number,
  hysteresisS: number,
  minAccuracyM: number,
  emails: string[],
  notifyOnTransition: boolean,
): VacationModeSettingsOut {
  return {
    enabled,
    user_ids: userIds,
    min_distance_m: minDistanceM,
    hysteresis_s: hysteresisS,
    min_location_accuracy_m: minAccuracyM,
    notification_emails: emails,
    notify_on_transition: notifyOnTransition,
  };
}

export async function mountVacationModeSettingsPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
): Promise<void> {
  container.replaceChildren();
  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  let current: VacationModeSettingsStatusOut | null = null;
  let users: UserOut[] = [];
  try {
    [current, users] = await Promise.all([
      dataSource.getVacationModeSettings(),
      dataSource.listUsers(),
    ]);
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not load vacation mode: ${formatError(err)}`;
  }

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Turns on when every selected person stays beyond the distance from home for the wait time; turns off when any of them enters the home geofence (Automations → Geofences / wifi_home_geofence_id). Optional emails on those edges. SMTP is under Mail.";

  const armedBadge = document.createElement("p");
  armedBadge.className = "rules-vacation-armed";
  armedBadge.textContent =
    current?.armed === true ? "Latch: on (armed)" : "Latch: off (disarmed)";

  const form = document.createElement("form");
  form.className = "rules-vacation-form";
  form.noValidate = true;

  const enabledSelect = document.createElement("select");
  const optEnabled = document.createElement("option");
  optEnabled.value = "true";
  optEnabled.textContent = "Enabled — evaluate far-from-home latch";
  const optDisabled = document.createElement("option");
  optDisabled.value = "false";
  optDisabled.textContent = "Disabled — leave latch unchanged";
  enabledSelect.append(optEnabled, optDisabled);
  enabledSelect.value = current?.enabled === true ? "true" : "false";
  const enabledField = document.createElement("label");
  enabledField.className = "settings-dialog-field";
  enabledField.append(createFieldLabel("Vacation mode"), enabledSelect);

  const usersField = document.createElement("fieldset");
  usersField.className = "settings-dialog-fieldset";
  const usersLegend = document.createElement("legend");
  usersLegend.textContent = "People (all must be far from home to arm)";
  usersField.append(usersLegend);
  const userChecks = new Map<string, HTMLInputElement>();
  const selected = new Set(current?.user_ids ?? []);
  for (const user of users) {
    const row = document.createElement("label");
    row.className = "settings-dialog-checkbox";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(user.user_id);
    userChecks.set(user.user_id, cb);
    const label = user.display_name.trim() !== "" ? user.display_name : user.user_id;
    row.append(cb, document.createTextNode(label));
    usersField.append(row);
  }
  if (users.length === 0) {
    const empty = document.createElement("p");
    empty.className = "settings-dialog-help";
    empty.textContent = "No users in the roster yet. Sync My Tracks under Users.";
    usersField.append(empty);
  }

  const distanceInput = document.createElement("input");
  distanceInput.type = "number";
  distanceInput.min = "1";
  distanceInput.step = "1";
  distanceInput.value = String(current?.min_distance_m ?? 80_000);

  const hysteresisInput = document.createElement("input");
  hysteresisInput.type = "number";
  hysteresisInput.min = "1";
  hysteresisInput.step = "1";
  hysteresisInput.value = String(current?.hysteresis_s ?? 1800);

  const accuracyInput = document.createElement("input");
  accuracyInput.type = "number";
  accuracyInput.min = "1";
  accuracyInput.step = "1";
  accuracyInput.value = String(current?.min_location_accuracy_m ?? 50);

  const emailsInput = document.createElement("input");
  emailsInput.type = "email";
  emailsInput.multiple = true;
  emailsInput.placeholder = "operator@example.com";
  emailsInput.value = (current?.notification_emails ?? []).join(", ");

  const notifyCb = document.createElement("input");
  notifyCb.type = "checkbox";
  notifyCb.checked = current?.notify_on_transition !== false;

  const distanceField = document.createElement("label");
  distanceField.className = "settings-dialog-field";
  distanceField.append(createFieldLabel("Min distance from home (m)"), distanceInput);

  const hysteresisField = document.createElement("label");
  hysteresisField.className = "settings-dialog-field";
  hysteresisField.append(createFieldLabel("Wait before arm (s)"), hysteresisInput);

  const accuracyField = document.createElement("label");
  accuracyField.className = "settings-dialog-field";
  accuracyField.append(createFieldLabel("Min location accuracy (m)"), accuracyInput);

  const tuningRow = document.createElement("div");
  tuningRow.className = "settings-dialog-field-row";
  tuningRow.append(distanceField, hysteresisField, accuracyField);

  const emailsField = document.createElement("label");
  emailsField.className = "settings-dialog-field";
  emailsField.append(createFieldLabel("Notification emails"), emailsInput);

  const notifyRow = document.createElement("label");
  notifyRow.className = "settings-dialog-checkbox";
  notifyRow.append(
    notifyCb,
    document.createTextNode("Email on arm and disarm (anomaly alerts while armed are separate)"),
  );

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  actions.append(saveBtn);

  const testRow = document.createElement("div");
  testRow.className = "rules-mail-test-row";
  const testArmed = document.createElement("select");
  const optOn = document.createElement("option");
  optOn.value = "true";
  optOn.textContent = "Sample: vacation mode on";
  const optOff = document.createElement("option");
  optOff.value = "false";
  optOff.textContent = "Sample: vacation mode off";
  testArmed.append(optOn, optOff);
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Send test email";
  testRow.append(testArmed, testBtn);

  form.append(
    enabledField,
    usersField,
    tuningRow,
    emailsField,
    notifyRow,
    actions,
    testRow,
  );
  container.append(lead, armedBadge, status, form);

  let busy = false;

  const syncActionEnabled = (): void => {
    const hasEmails = parseEmailList(emailsInput.value).length > 0;
    saveBtn.disabled = busy;
    testBtn.disabled = busy || !hasEmails;
    testArmed.disabled = busy;
    enabledSelect.disabled = busy;
  };
  syncActionEnabled();
  emailsInput.addEventListener("input", () => {
    syncActionEnabled();
  });

  const collectSettings = (): VacationModeSettingsOut | null => {
    const userIds = [...userChecks.entries()]
      .filter(([, cb]) => cb.checked)
      .map(([id]) => id);
    const minDistance = Number(distanceInput.value);
    const hysteresis = Number(hysteresisInput.value);
    const accuracy = Number(accuracyInput.value);
    if (!Number.isFinite(minDistance) || minDistance <= 0) {
      status.hidden = false;
      status.textContent = "Expected min distance > 0";
      return null;
    }
    if (!Number.isFinite(hysteresis) || hysteresis < 1) {
      status.hidden = false;
      status.textContent = "Expected wait time ≥ 1 second";
      return null;
    }
    if (!Number.isFinite(accuracy) || accuracy < 1) {
      status.hidden = false;
      status.textContent = "Expected min location accuracy ≥ 1";
      return null;
    }
    return settingsFromForm(
      enabledSelect.value === "true",
      userIds,
      minDistance,
      hysteresis,
      Math.trunc(accuracy),
      parseEmailList(emailsInput.value),
      notifyCb.checked,
    );
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const settings = collectSettings();
    if (settings === null) {
      return;
    }
    busy = true;
    syncActionEnabled();
    status.hidden = false;
    status.textContent = "Saving…";
    void dataSource
      .saveVacationModeSettings(settings)
      .then((saved) => {
        armedBadge.textContent =
          saved.armed === true ? "Latch: on (armed)" : "Latch: off (disarmed)";
        status.textContent = "Saved.";
        showSuccessToast("Vacation mode settings saved.");
      })
      .catch((err: unknown) => {
        status.textContent = `Save failed: ${formatError(err)}`;
      })
      .finally(() => {
        busy = false;
        syncActionEnabled();
      });
  });

  testBtn.addEventListener("click", () => {
    if (parseEmailList(emailsInput.value).length === 0) {
      status.hidden = false;
      status.textContent = "Add at least one notification email before testing.";
      return;
    }
    busy = true;
    syncActionEnabled();
    status.hidden = false;
    status.textContent = "Sending test email…";
    const armed = testArmed.value === "true";
    void dataSource
      .sendVacationModeTestEmail({ armed })
      .then((result) => {
        if (result.ok) {
          status.textContent = result.message;
          showSuccessToast(result.message);
        } else {
          status.textContent = `Test failed: ${result.message}`;
        }
      })
      .catch((err: unknown) => {
        status.textContent = `Test failed: ${formatError(err)}`;
      })
      .finally(() => {
        busy = false;
        syncActionEnabled();
      });
  });
}
