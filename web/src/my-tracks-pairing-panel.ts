// My Tracks domesti-bot pairing and location-history retention settings.

import { api, HttpError } from "./api.js";
import { setAuditedTimestampLine } from "./format-timestamp.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { confirmAction, showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  LocationHistoryRetentionIn,
  MyTracksPairIn,
  MyTracksPairStatusOut,
  MyTracksSettingsIn,
  MyTracksSettingsOut,
} from "./types.js";

export interface MyTracksPairingPanelOptions {
  clearConnectionFields: () => void;
  readConnectionSettings: () => MyTracksSettingsIn;
  resetAllSettings: () => Promise<void>;
  saveConnectionSettings: (config: MyTracksSettingsIn) => Promise<MyTracksSettingsOut>;
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

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

function readRetentionFromForm(options: {
  maxAgeHoursInput: HTMLInputElement;
  minKeepCountInput: HTMLInputElement;
  unlimitedInput: HTMLInputElement;
}): LocationHistoryRetentionIn {
  return {
    max_age_hours: Number.parseFloat(options.maxAgeHoursInput.value),
    min_keep_count: Number.parseInt(options.minKeepCountInput.value, 10),
    unlimited: options.unlimitedInput.checked,
  };
}

function retentionEquals(
  left: LocationHistoryRetentionIn,
  right: LocationHistoryRetentionIn,
): boolean {
  return (
    left.unlimited === right.unlimited
    && left.max_age_hours === right.max_age_hours
    && left.min_keep_count === right.min_keep_count
  );
}

function setRetentionInputsEnabled(options: {
  enabled: boolean;
  maxAgeHoursInput: HTMLInputElement;
  minKeepCountInput: HTMLInputElement;
}): void {
  options.maxAgeHoursInput.disabled = !options.enabled;
  options.minKeepCountInput.disabled = !options.enabled;
}

function applyRetentionToForm(
  retention: LocationHistoryRetentionIn,
  options: {
    maxAgeHoursInput: HTMLInputElement;
    minKeepCountInput: HTMLInputElement;
    unlimitedInput: HTMLInputElement;
  },
): void {
  options.unlimitedInput.checked = retention.unlimited;
  options.maxAgeHoursInput.value = String(retention.max_age_hours);
  options.minKeepCountInput.value = String(retention.min_keep_count);
  setRetentionInputsEnabled({
    enabled: !retention.unlimited,
    maxAgeHoursInput: options.maxAgeHoursInput,
    minKeepCountInput: options.minKeepCountInput,
  });
}

function renderPairStatus(statusEl: HTMLElement, status: MyTracksPairStatusOut | null): void {
  if (status?.paired_at) {
    statusEl.hidden = false;
    const remoteRequests = status.mytracks_remote_request_location_enabled;
    const remoteLabel = remoteRequests === true
      ? "Remote location requests: enabled on my-tracks"
      : remoteRequests === false
        ? "Remote location requests: disabled on my-tracks"
        : "Remote location requests: unknown (re-pair with admin password to refresh)";
    setAuditedTimestampLine(statusEl, {
      iso: status.paired_at,
      prefix: "Paired at ",
      suffix: ` · ${remoteLabel}`,
    });
    return;
  }
  if (status?.last_pair_error) {
    statusEl.hidden = false;
    statusEl.textContent = `Last pairing failed: ${status.last_pair_error}`;
    return;
  }
  statusEl.hidden = true;
  statusEl.textContent = "";
}

function updatePairButtonLabel(
  pairBtn: HTMLButtonElement,
  status: MyTracksPairStatusOut | null,
): void {
  pairBtn.textContent = status?.paired_at ? "Re-pair" : "Pair";
}

function updateResetButton(
  resetBtn: HTMLButtonElement,
  options: {
    pairStatus: MyTracksPairStatusOut | null;
    storedSettings: MyTracksSettingsOut | null;
  },
): void {
  const hasSettings = options.storedSettings !== null
    || options.pairStatus?.paired_at !== null
    || options.pairStatus?.relay_key_configured === true;
  resetBtn.disabled = !hasSettings;
  resetBtn.title = hasSettings ? "" : "No settings to reset";
}

function updateSaveRetentionButton(
  saveRetentionBtn: HTMLButtonElement,
  current: LocationHistoryRetentionIn,
  saved: LocationHistoryRetentionIn | null,
): void {
  const unchanged = saved !== null && retentionEquals(current, saved);
  saveRetentionBtn.disabled = unchanged;
  saveRetentionBtn.title = unchanged ? "No changes to save" : "";
}

function validateConnectionSettings(settings: MyTracksSettingsIn): string | null {
  if (settings.domain === "") {
    return "Enter a My Tracks domain before pairing.";
  }
  if (settings.username === "") {
    return "Enter a default admin username before pairing.";
  }
  return null;
}

async function promptPairPassword(
  username: string,
  rePair: boolean,
): Promise<string | null> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "settings-dialog mytracks-sync-dialog";

    const panel = document.createElement("div");
    panel.className = "settings-dialog-panel";

    const header = document.createElement("header");
    header.className = "settings-dialog-header mytracks-sync-header";
    const title = document.createElement("h2");
    title.textContent = rePair ? "Re-pair with My Tracks" : "Pair with My Tracks";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "settings-dialog-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "\u00d7";
    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.className = "settings-dialog-body mytracks-sync-body";
    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead";
    lead.textContent = `Enter the My Tracks admin password for ${username}.`;

    const panelForm = document.createElement("div");
    panelForm.className = "mytracks-sync-form";
    const passwordRow = createSecretInputRow({
      autocomplete: "off",
      required: true,
    });
    passwordRow.input.name = `mytracks-pair-secret-${crypto.randomUUID()}`;
    preventBrowserAutofill(passwordRow.input);
    appendLabeledField(
      panelForm,
      createFieldLabel("Admin password"),
      passwordRow.row,
    );

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const submitBtn = document.createElement("button");
    submitBtn.type = "button";
    submitBtn.className = "btn";
    submitBtn.textContent = rePair ? "Re-pair" : "Pair";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-secondary";
    cancelBtn.textContent = "Cancel";
    actions.append(submitBtn, cancelBtn);
    panelForm.append(actions);

    body.append(lead, panelForm);
    panel.append(header, body);
    dialog.append(panel);
    document.body.append(dialog);

    const finish = (password: string | null): void => {
      dialog.close();
      dialog.remove();
      resolve(password);
    };

    closeBtn.addEventListener("click", () => {
      finish(null);
    });
    cancelBtn.addEventListener("click", () => {
      finish(null);
    });
    dialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      finish(null);
    });
    dialog.addEventListener("click", (ev) => {
      if (ev.target === dialog) {
        finish(null);
      }
    });
    const submitPassword = (): void => {
      const password = passwordRow.input.value;
      if (password.trim() === "") {
        return;
      }
      finish(password);
    };
    submitBtn.addEventListener("click", submitPassword);
    passwordRow.input.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") {
        ev.preventDefault();
        submitPassword();
      }
    });

    dialog.showModal();
    passwordRow.input.focus();
  });
}

export async function mountMyTracksPairingPanel(
  container: HTMLElement,
  options: MyTracksPairingPanelOptions,
): Promise<void> {
  const section = document.createElement("section");
  section.className = "mytracks-pairing-section";

  const heading = document.createElement("h3");
  heading.className = "settings-dialog-subheading";
  heading.textContent = "Pairing";

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Register domesti-bot webhook URLs and a relay secret on my-tracks. " +
    "The public domesti-bot URL is derived from this browser session.";

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const form = document.createElement("form");
  form.className = "mytracks-pairing-form";
  form.noValidate = true;
  form.setAttribute("autocomplete", "off");

  const relayKeyField = document.createElement("div");
  relayKeyField.className = "settings-dialog-field mytracks-relay-key-field";
  const relayKeyLabel = createFieldLabel("Relay API key");
  const relayKeyPending = document.createElement("p");
  relayKeyPending.className = "settings-dialog-help mytracks-relay-key-pending";
  relayKeyPending.textContent = "Will be populated when pairing is complete.";
  const relayKeyRow = createSecretInputRow({ autocomplete: "off" });
  relayKeyRow.input.readOnly = true;
  relayKeyRow.input.tabIndex = -1;
  relayKeyRow.row.hidden = true;
  relayKeyField.append(relayKeyLabel, relayKeyPending, relayKeyRow.row);
  form.append(relayKeyField);

  const retentionGroup = document.createElement("fieldset");
  retentionGroup.className = "settings-dialog-fieldset";
  const retentionLegend = document.createElement("legend");
  retentionLegend.textContent = "Location history per user";
  retentionGroup.append(retentionLegend);

  const retentionHelp = document.createElement("p");
  retentionHelp.className = "settings-dialog-help";
  retentionHelp.textContent =
    "Default keeps per-user locations from the last 24 hours and always keeps " +
    "at least the 20 most recent per-user locations.";
  retentionGroup.append(retentionHelp);

  const unlimitedInput = document.createElement("input");
  unlimitedInput.type = "checkbox";
  unlimitedInput.id = "mytracks-location-history-unlimited";
  const unlimitedLabel = document.createElement("label");
  unlimitedLabel.className = "settings-dialog-checkbox";
  unlimitedLabel.htmlFor = unlimitedInput.id;
  unlimitedLabel.append(unlimitedInput, document.createTextNode(" Keep all location history"));
  retentionGroup.append(unlimitedLabel);

  const retentionRow = document.createElement("div");
  retentionRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const maxAgeHoursInput = document.createElement("input");
  maxAgeHoursInput.type = "number";
  maxAgeHoursInput.min = "1";
  maxAgeHoursInput.step = "1";
  maxAgeHoursInput.required = true;
  maxAgeHoursInput.value = "24";
  appendLabeledField(
    retentionRow,
    createFieldLabel("Keep per-user locations from the last (hours)"),
    maxAgeHoursInput,
  );

  const minKeepCountInput = document.createElement("input");
  minKeepCountInput.type = "number";
  minKeepCountInput.min = "1";
  minKeepCountInput.step = "1";
  minKeepCountInput.required = true;
  minKeepCountInput.value = "20";
  appendLabeledField(
    retentionRow,
    createFieldLabel("Minimum recent per-user locations to keep"),
    minKeepCountInput,
  );
  retentionGroup.append(retentionRow);
  form.append(retentionGroup);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const pairBtn = document.createElement("button");
  pairBtn.type = "button";
  pairBtn.className = "btn";
  pairBtn.textContent = "Pair";
  const saveRetentionBtn = document.createElement("button");
  saveRetentionBtn.type = "button";
  saveRetentionBtn.className = "btn btn-secondary";
  saveRetentionBtn.textContent = "Save retention";
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "btn btn-secondary";
  resetBtn.textContent = "Reset";
  actions.append(pairBtn, saveRetentionBtn, resetBtn);
  form.append(actions);

  section.append(heading, lead, status, form);
  container.append(section);

  let pairStatus: MyTracksPairStatusOut | null = null;
  let savedRetention: LocationHistoryRetentionIn | null = null;
  let storedConnection: MyTracksSettingsOut | null = null;
  let storedRelayKey: string | null = null;
  let relayKeyRevealed = false;

  const retentionControls = {
    maxAgeHoursInput,
    minKeepCountInput,
    unlimitedInput,
  };

  const setRelayKeyRevealed = (revealed: boolean): void => {
    relayKeyRevealed = revealed;
    if (revealed && relayKeyRow.input.value === "" && storedRelayKey) {
      relayKeyRow.input.value = storedRelayKey;
    }
    relayKeyRow.setRevealed(revealed);
  };
  setRelayKeyRevealed(false);

  const applyRelayKeyDisplay = (): void => {
    const showKey = pairStatus?.paired_at !== null && pairStatus?.paired_at !== undefined
      && storedRelayKey !== null;
    relayKeyPending.hidden = showKey;
    relayKeyRow.row.hidden = !showKey;
    if (storedRelayKey) {
      relayKeyRow.input.value = storedRelayKey;
      if (!relayKeyRevealed) {
        relayKeyRow.input.type = "password";
      }
    } else {
      relayKeyRow.input.value = "";
      setRelayKeyRevealed(false);
    }
  };

  const syncResetButtonState = (): void => {
    updateResetButton(resetBtn, {
      pairStatus,
      storedSettings: storedConnection,
    });
  };

  const syncRetentionSaveState = (): void => {
    updateSaveRetentionButton(
      saveRetentionBtn,
      readRetentionFromForm(retentionControls),
      savedRetention,
    );
  };

  unlimitedInput.addEventListener("change", () => {
    setRetentionInputsEnabled({
      enabled: !unlimitedInput.checked,
      maxAgeHoursInput,
      minKeepCountInput,
    });
    syncRetentionSaveState();
  });
  maxAgeHoursInput.addEventListener("input", syncRetentionSaveState);
  minKeepCountInput.addEventListener("input", syncRetentionSaveState);

  async function refreshStatus(): Promise<void> {
    try {
      const [nextPairStatus, nextConnection] = await Promise.all([
        api.fetchMyTracksPairStatus(),
        api.fetchMyTracksSettings(),
      ]);
      pairStatus = nextPairStatus;
      storedConnection = nextConnection;
      if (pairStatus?.location_history_retention) {
        savedRetention = { ...pairStatus.location_history_retention };
        applyRetentionToForm(pairStatus.location_history_retention, retentionControls);
      }
      renderPairStatus(status, pairStatus);
      updatePairButtonLabel(pairBtn, pairStatus);
      if (pairStatus?.paired_at) {
        const relaySettings = await api.fetchMyTracksRelayKeySettings();
        storedRelayKey = relaySettings.stored_relay_key;
      } else {
        storedRelayKey = null;
      }
      applyRelayKeyDisplay();
      syncRetentionSaveState();
      syncResetButtonState();
    } catch (err) {
      status.textContent = `Could not load pairing status: ${formatError(err)}`;
    }
  }

  pairBtn.addEventListener("click", () => {
    void (async () => {
      const connection = options.readConnectionSettings();
      const validationError = validateConnectionSettings(connection);
      if (validationError !== null) {
        showErrorToast(validationError);
        return;
      }
      const rePair = pairStatus?.paired_at !== null && pairStatus?.paired_at !== undefined;
      const password = await promptPairPassword(connection.username, rePair);
      if (password === null) {
        return;
      }
      let savedConnection: MyTracksSettingsOut;
      try {
        savedConnection = await options.saveConnectionSettings(connection);
      } catch (err) {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
        return;
      }
      const payload: MyTracksPairIn = {
        domain: savedConnection.domain,
        location_history_retention: readRetentionFromForm(retentionControls),
        password,
        username: savedConnection.username,
      };
      try {
        pairStatus = await api.postMyTracksPair(payload);
        storedConnection = savedConnection;
        savedRetention = { ...pairStatus.location_history_retention };
        renderPairStatus(status, pairStatus);
        updatePairButtonLabel(pairBtn, pairStatus);
        const relaySettings = await api.fetchMyTracksRelayKeySettings();
        storedRelayKey = relaySettings.stored_relay_key;
        applyRelayKeyDisplay();
        syncRetentionSaveState();
        syncResetButtonState();
        showSuccessToast(rePair ? "My Tracks re-pairing complete." : "My Tracks pairing complete.");
      } catch (err) {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
        await refreshStatus();
      }
    })();
  });

  saveRetentionBtn.addEventListener("click", () => {
    void api
      .patchMyTracksLocationHistoryRetention(readRetentionFromForm(retentionControls))
      .then((saved) => {
        savedRetention = { ...saved };
        applyRetentionToForm(saved, retentionControls);
        syncRetentionSaveState();
        showSuccessToast("Location history retention saved.");
      })
      .catch((err: unknown) => {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
      });
  });

  resetBtn.addEventListener("click", () => {
    void confirmAction({
      title: "Reset My Tracks settings?",
      message:
        "This clears the domain, admin username, relay API key, and pairing metadata on domesti-bot. " +
        "my-tracks may still accept the old relay key until you re-pair or clear it there.",
      confirmLabel: "Reset",
      variant: "danger",
    }).then((confirmed) => {
      if (!confirmed) {
        return;
      }
      void options
        .resetAllSettings()
        .then(() => {
          options.clearConnectionFields();
          pairStatus = null;
          storedConnection = null;
          storedRelayKey = null;
          savedRetention = null;
          renderPairStatus(status, pairStatus);
          updatePairButtonLabel(pairBtn, pairStatus);
          applyRelayKeyDisplay();
          applyRetentionToForm(
            { max_age_hours: 24, min_keep_count: 20, unlimited: false },
            retentionControls,
          );
          syncRetentionSaveState();
          syncResetButtonState();
          showSuccessToast("My Tracks settings reset.");
        })
        .catch((err: unknown) => {
          const message = formatError(err);
          status.textContent = message;
          showErrorToast(message);
        });
    });
  });

  await refreshStatus();
}
