// Vacation mode settings panel (Automations hub).

import { HttpError } from "./api.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel } from "./rules-ui-helpers.js";
import type {
  UserOut,
  VacationModeSettingsOut,
  VacationModeSettingsStatusOut,
} from "./types.js";

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
): VacationModeSettingsOut {
  return {
    enabled,
    user_ids: userIds,
    min_distance_m: minDistanceM,
    hysteresis_s: hysteresisS,
    min_location_accuracy_m: minAccuracyM,
    notification_emails: emails,
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
    "Sticky far-from-home latch. Arms when every selected user stays beyond the distance for the hysteresis window; emails recipients on arm and disarm. Configure SMTP under Mail.";

  const armedBadge = document.createElement("p");
  armedBadge.className = "settings-dialog-status";
  armedBadge.textContent =
    current?.armed === true ? "Status: armed" : "Status: disarmed";

  const form = document.createElement("form");
  form.className = "rules-mail-form";
  form.noValidate = true;

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "settings-dialog-field";
  const enabledCb = document.createElement("input");
  enabledCb.type = "checkbox";
  enabledCb.checked = current?.enabled ?? false;
  enabledLabel.append(enabledCb, document.createTextNode(" Enabled"));

  const usersField = document.createElement("fieldset");
  usersField.className = "settings-dialog-field";
  const usersLegend = document.createElement("legend");
  usersLegend.textContent = "Users (all must be far from home)";
  usersField.append(usersLegend);
  const userChecks = new Map<string, HTMLInputElement>();
  const selected = new Set(current?.user_ids ?? []);
  for (const user of users) {
    const row = document.createElement("label");
    row.className = "settings-dialog-field";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = selected.has(user.user_id);
    userChecks.set(user.user_id, cb);
    const label = user.display_name.trim() !== "" ? user.display_name : user.user_id;
    row.append(cb, document.createTextNode(` ${label}`));
    usersField.append(row);
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

  const distanceField = document.createElement("label");
  distanceField.className = "settings-dialog-field";
  distanceField.append(createFieldLabel("Min distance from home (m)"), distanceInput);

  const hysteresisField = document.createElement("label");
  hysteresisField.className = "settings-dialog-field";
  hysteresisField.append(createFieldLabel("Hysteresis (seconds)"), hysteresisInput);

  const accuracyField = document.createElement("label");
  accuracyField.className = "settings-dialog-field";
  accuracyField.append(createFieldLabel("Min location accuracy (m)"), accuracyInput);

  const emailsField = document.createElement("label");
  emailsField.className = "settings-dialog-field";
  emailsField.append(createFieldLabel("Notification emails"), emailsInput);

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
  testBtn.className = "btn";
  testBtn.textContent = "Send test email";
  testRow.append(testArmed, testBtn);

  form.append(
    enabledLabel,
    usersField,
    distanceField,
    hysteresisField,
    accuracyField,
    emailsField,
    actions,
    testRow,
  );
  container.append(lead, armedBadge, status, form);

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
      status.textContent = "Expected hysteresis ≥ 1 second";
      return null;
    }
    if (!Number.isFinite(accuracy) || accuracy < 1) {
      status.hidden = false;
      status.textContent = "Expected min location accuracy ≥ 1";
      return null;
    }
    return settingsFromForm(
      enabledCb.checked,
      userIds,
      minDistance,
      hysteresis,
      Math.trunc(accuracy),
      parseEmailList(emailsInput.value),
    );
  };

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const settings = collectSettings();
    if (settings === null) {
      return;
    }
    status.hidden = false;
    status.textContent = "Saving…";
    void dataSource
      .saveVacationModeSettings(settings)
      .then((saved) => {
        armedBadge.textContent =
          saved.armed === true ? "Status: armed" : "Status: disarmed";
        status.textContent = "Saved vacation mode settings.";
      })
      .catch((err: unknown) => {
        status.textContent = `Save failed: ${formatError(err)}`;
      });
  });

  testBtn.addEventListener("click", () => {
    status.hidden = false;
    status.textContent = "Sending test email…";
    const armed = testArmed.value === "true";
    void dataSource
      .sendVacationModeTestEmail({ armed })
      .then((result) => {
        status.textContent = result.ok
          ? result.message
          : `Test failed: ${result.message}`;
      })
      .catch((err: unknown) => {
        status.textContent = `Test failed: ${formatError(err)}`;
      });
  });
}
