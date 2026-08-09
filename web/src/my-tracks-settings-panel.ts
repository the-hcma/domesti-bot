// My Tracks connection settings (domain + default admin username).

import { HttpError, api } from "./api.js";
import { ToastVariant } from "./closed-sets.js";
import { mountMyTracksPairingPanel } from "./my-tracks-pairing-panel.js";
import { appendMyTracksInstanceText, myTracksHostLabel } from "./mytracks-ui-helpers.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
import { setSettingsDialogStatus } from "./settings-status.js";
import type { MyTracksSettingsIn } from "./types.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";

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

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

export async function mountMyTracksSettingsPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
): Promise<void> {
  container.replaceChildren();
  container.classList.add("mytracks-settings-tab");

  const status = document.createElement("p");
  status.className = "settings-dialog-status";

  const showStatusMessage = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(status, message, tone);
  };

  status.hidden = true;

  let existing = null as Awaited<ReturnType<RulesDataSource["getMyTracksSettings"]>>;
  try {
    existing = await dataSource.getMyTracksSettings();
  } catch (err) {
    showStatusMessage(
      `Could not load My Tracks settings: ${formatError(err)}`,
      ToastVariant.Error,
    );
  }

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  appendMyTracksInstanceText(lead, {
    before: "Connect to ",
    domain: existing?.domain ?? "",
    after:
      " for user and geofence sync. Pairing saves the domain and admin username; sync prompts for the admin password each time — it is not stored.",
  });

  const connectionSection = document.createElement("div");
  connectionSection.className = "mytracks-connection-section";

  const fieldsRow = document.createElement("div");
  fieldsRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const domainInput = document.createElement("input");
  domainInput.type = "text";
  domainInput.inputMode = "url";
  domainInput.placeholder = "tracks.example.com";
  domainInput.required = true;
  domainInput.value = existing?.domain ? myTracksHostLabel(existing.domain) : "";
  domainInput.spellcheck = false;
  preventBrowserAutofill(domainInput);
  appendLabeledField(
    fieldsRow,
    createFieldLabel("My Tracks domain", {
      detail: "Only HTTPS is supported.",
      example: "mytracks.example.com",
    }),
    domainInput,
  );

  const usernameInput = document.createElement("input");
  usernameInput.type = "text";
  usernameInput.name = "mytracks-default-admin";
  preventBrowserAutofill(usernameInput);
  usernameInput.required = true;
  usernameInput.value = existing?.username ?? "";
  appendLabeledField(
    fieldsRow,
    createFieldLabel("Default admin username"),
    usernameInput,
  );

  const passwordInput = document.createElement("input");
  passwordInput.type = "password";
  passwordInput.name = "mytracks-test-password";
  passwordInput.autocomplete = "current-password";
  passwordInput.placeholder = "Required for Test (never stored)";
  preventBrowserAutofill(passwordInput);

  const connectionActions = document.createElement("div");
  connectionActions.className = "settings-dialog-actions";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Test";
  testBtn.disabled = true;
  connectionActions.append(testBtn);
  connectionSection.append(fieldsRow);
  appendLabeledField(
    connectionSection,
    createFieldLabel("Admin password", {
      detail: "Used only for Test; never saved.",
      example: "your My Tracks admin password",
    }),
    passwordInput,
  );
  connectionSection.append(connectionActions);

  const syncTestEnabled = (): void => {
    testBtn.disabled = !(
      passwordInput.value !== "" &&
      domainInput.value.trim() !== "" &&
      usernameInput.value.trim() !== ""
    );
  };
  domainInput.addEventListener("input", () => {
    syncTestEnabled();
  });
  usernameInput.addEventListener("input", () => {
    syncTestEnabled();
  });
  passwordInput.addEventListener("input", () => {
    syncTestEnabled();
  });
  syncTestEnabled();

  testBtn.addEventListener("click", () => {
    const domain = domainInput.value.trim();
    const username = usernameInput.value.trim();
    const password = passwordInput.value;
    if (password === "") {
      showStatusMessage(
        "Enter the admin password before testing.",
        ToastVariant.Error,
      );
      return;
    }
    if (domain === "" || username === "") {
      showStatusMessage(
        "Enter domain and username before testing.",
        ToastVariant.Error,
      );
      return;
    }
    testBtn.disabled = true;
    showStatusMessage("Testing credentials…", ToastVariant.Info);
    void api
      .testMyTracksCredentials({ domain, username, password })
      .then((result) => {
        showStatusMessage(
          result.detail,
          result.ok ? ToastVariant.Success : ToastVariant.Error,
        );
      })
      .catch((err: unknown) => {
        showStatusMessage(formatError(err), ToastVariant.Error);
      })
      .finally(() => {
        syncTestEnabled();
      });
  });

  const monitoringSection = document.createElement("div");
  monitoringSection.className = "mytracks-monitoring-section";

  const monitoringHeading = document.createElement("h3");
  monitoringHeading.className = "settings-dialog-subheading";
  monitoringHeading.textContent = "Location monitoring";

  const monitoringHelp = document.createElement("p");
  monitoringHelp.className = "settings-dialog-help";
  monitoringHelp.textContent =
    "Start high-cadence location requests when a user is outside a geofence but within " +
    "this distance of its edge (for geofences used by enabled arrival/departure rules).";

  const approachDistanceInput = document.createElement("input");
  approachDistanceInput.type = "number";
  approachDistanceInput.min = "50";
  approachDistanceInput.step = "1";
  approachDistanceInput.required = true;
  approachDistanceInput.value = "500";

  const monitoringFieldsRow = document.createElement("div");
  monitoringFieldsRow.className = "settings-dialog-field-row mytracks-settings-fields-row";
  appendLabeledField(
    monitoringFieldsRow,
    createFieldLabel("Approach monitoring distance (m)"),
    approachDistanceInput,
  );

  const saveMonitoringBtn = document.createElement("button");
  saveMonitoringBtn.type = "button";
  saveMonitoringBtn.className = "btn btn-secondary";
  saveMonitoringBtn.textContent = "Save approach distance";

  monitoringSection.append(
    monitoringHeading,
    monitoringHelp,
    monitoringFieldsRow,
    saveMonitoringBtn,
  );

  try {
    const monitoring = await api.fetchMyTracksLocationMonitoring();
    approachDistanceInput.value = String(monitoring.approach_distance_m);
  } catch {
    // Settings hub still loads when monitoring endpoint is unavailable.
  }

  saveMonitoringBtn.addEventListener("click", () => {
    const distance = approachDistanceInput.valueAsNumber;
    if (!Number.isInteger(distance) || distance < 50 || distance > 10_000) {
      showErrorToast("Enter an approach monitoring distance between 50 and 10000 meters.");
      return;
    }
    saveMonitoringBtn.disabled = true;
    void api
      .patchMyTracksLocationMonitoring({
        approach_distance_m: distance,
      })
      .then((saved) => {
        approachDistanceInput.value = String(saved.approach_distance_m);
        showSuccessToast("Approach monitoring distance saved.");
      })
      .catch((err: unknown) => {
        showErrorToast(formatError(err));
      })
      .finally(() => {
        saveMonitoringBtn.disabled = false;
      });
  });

  container.append(lead, status, connectionSection, monitoringSection);

  const pairingMount = document.createElement("div");
  pairingMount.className = "mytracks-pairing-mount";
  container.append(pairingMount);

  const readConnectionSettings = (): MyTracksSettingsIn => {
    const host = domainInput.value.trim();
    return {
      domain: host === "" ? "" : host,
      username: usernameInput.value.trim(),
    };
  };

  const clearConnectionFields = (): void => {
    domainInput.value = "";
    usernameInput.value = "";
    passwordInput.value = "";
    syncTestEnabled();
  };

  await mountMyTracksPairingPanel(pairingMount, {
    clearConnectionFields,
    readConnectionSettings,
    resetAllSettings: () => dataSource.resetMyTracksSettings(),
    saveConnectionSettings: (config) => dataSource.saveMyTracksSettings(config),
  });
}
