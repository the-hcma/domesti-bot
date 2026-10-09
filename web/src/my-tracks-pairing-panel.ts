// My Tracks domesti-bot pairing and location-history retention settings.

import { api, HttpError } from "./api.js";
import { ToastVariant } from "./closed-sets.js";
import { setAuditedTimestampLine } from "./format-timestamp.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import {
  setSettingsDialogStatus,
  setSettingsDialogStatusTone,
} from "./settings-status.js";
import {
  ConfirmButtonVariant,
  type LocationHistoryRetentionIn,
  type MyTracksPairIn,
  type MyTracksPairStatusOut,
  type MyTracksSettingsIn,
  type MyTracksSettingsOut,
} from "./types.js";
import { confirmAction, showErrorToast, showInfoToast, showSuccessToast } from "./ui-toast.js";

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

function effectiveUserCooldownSeconds(
  limits: MyTracksPairStatusOut["mytracks_location_request_rate_limits"],
  reason: string,
): number | null {
  if (limits === null) {
    return null;
  }
  const tiered = limits.user_cooldown_seconds_by_reason?.[reason];
  return tiered ?? limits.user_cooldown_seconds;
}

function renderPairStatus(
  statusEl: HTMLElement,
  status: MyTracksPairStatusOut | null,
  approachRequestIntervalS: number | null,
): void {
  if (status?.paired_at) {
    statusEl.hidden = false;
    setSettingsDialogStatusTone(statusEl, ToastVariant.Info);
    statusEl.replaceChildren();
    const remoteRequests = status.mytracks_remote_request_location_enabled;
    const remoteLabel = remoteRequests === true
      ? "Remote location requests: enabled on my-tracks"
      : remoteRequests === false
        ? "Remote location requests: disabled on my-tracks"
        : "Remote location requests: unknown (re-pair with admin password to refresh)";
    const pairedLine = document.createElement("span");
    setAuditedTimestampLine(pairedLine, {
      iso: status.paired_at,
      prefix: "Paired at ",
      suffix: ` · ${remoteLabel}`,
    });
    statusEl.append(pairedLine);
    const rateLimits = status.mytracks_location_request_rate_limits;
    const limitsLine = document.createElement("span");
    limitsLine.className = "settings-dialog-help";
    if (rateLimits === null) {
      limitsLine.textContent =
        "Location request cooldowns (my-tracks): unknown (re-pair with admin password to refresh)";
    } else {
      limitsLine.textContent =
        `Location request cooldowns (my-tracks): per-user ${String(rateLimits.user_cooldown_seconds)} s · ` +
        `per-device ${String(rateLimits.device_cooldown_seconds)} s`;
    }
    statusEl.append(document.createElement("br"), limitsLine);
    if (status.last_pair_error) {
      // A failed (re-)pair leaves the previous pairing in place, but it must stay visible: after a failure that
      // came after my-tracks accepted a new key, webhooks fail until the next successful pair.
      const failed = document.createElement("span");
      failed.className = "settings-dialog-help";
      failed.textContent = `Last pairing failed: ${status.last_pair_error}`;
      statusEl.append(document.createElement("br"), failed);
    }
    if (approachRequestIntervalS !== null && rateLimits !== null) {
      const approachCooldown = effectiveUserCooldownSeconds(rateLimits, "approach_monitoring");
      if (
        approachCooldown !== null
        && approachRequestIntervalS < approachCooldown
      ) {
        const warn = document.createElement("span");
        warn.className = "settings-dialog-help";
        warn.textContent =
          `Approach monitoring is configured for ${String(approachRequestIntervalS)} s requests, ` +
          `but my-tracks allows at most one per-user request every ${String(approachCooldown)} s.`;
        statusEl.append(document.createElement("br"), warn);
      }
    }
    return;
  }
  if (status?.last_pair_error) {
    setSettingsDialogStatus(
      statusEl,
      `Last pairing failed: ${status.last_pair_error}`,
      ToastVariant.Error,
    );
    return;
  }
  statusEl.hidden = true;
  statusEl.textContent = "";
  setSettingsDialogStatusTone(statusEl, null);
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

const RELAY_KEY_PENDING_NOTE = "Created automatically when pairing completes. It is never shown.";
const RELAY_KEY_CONFIGURED_NOTE =
  "Saved and delivered to My Tracks automatically. It is never shown; re-pair to replace it.";

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

  // The relay key is generated and delivered to My Tracks automatically during pairing, so it is never
  // shown or returned by the API; re-pairing replaces it.
  const relayKeyField = document.createElement("div");
  relayKeyField.className = "settings-dialog-field mytracks-relay-key-field";
  const relayKeyLabel = createFieldLabel("Relay API key");
  const relayKeyNote = document.createElement("p");
  relayKeyNote.className = "settings-dialog-help";
  relayKeyNote.textContent = RELAY_KEY_PENDING_NOTE;
  const relayProtocolNote = document.createElement("p");
  relayProtocolNote.className = "settings-dialog-help mytracks-relay-protocol-note";
  relayProtocolNote.hidden = true;
  const requireV2Input = document.createElement("input");
  requireV2Input.type = "checkbox";
  requireV2Input.id = "mytracks-require-relay-protocol-2";
  const requireV2Label = document.createElement("label");
  requireV2Label.className = "settings-dialog-checkbox";
  requireV2Label.htmlFor = requireV2Input.id;
  requireV2Label.append(
    requireV2Input,
    document.createTextNode(" Require relay protocol 2 (refuse to pair with a My Tracks that does not support it)"),
  );
  relayKeyField.append(relayKeyLabel, relayKeyNote, relayProtocolNote, requireV2Label);
  form.append(relayKeyField);

  // Plain-HTTP warnings for the pairing addresses (the relay keys are not encrypted on that hop).
  const transportNote = document.createElement("p");
  transportNote.className = "settings-dialog-help mytracks-transport-warning";
  transportNote.hidden = true;
  transportNote.setAttribute("role", "note");
  relayKeyField.append(transportNote);

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
  const checkActivationBtn = document.createElement("button");
  checkActivationBtn.type = "button";
  checkActivationBtn.className = "btn btn-secondary";
  checkActivationBtn.textContent = "Check activation";
  checkActivationBtn.hidden = true;
  const revokePreviousBtn = document.createElement("button");
  revokePreviousBtn.type = "button";
  revokePreviousBtn.className = "btn btn-secondary";
  revokePreviousBtn.textContent = "Revoke previous key";
  revokePreviousBtn.hidden = true;
  actions.append(pairBtn, checkActivationBtn, revokePreviousBtn, saveRetentionBtn, resetBtn);
  form.append(actions);

  section.append(heading, lead, status, form);
  container.append(section);

  let pairStatus: MyTracksPairStatusOut | null = null;
  let approachRequestIntervalS: number | null = null;
  let savedRetention: LocationHistoryRetentionIn | null = null;
  let storedConnection: MyTracksSettingsOut | null = null;

  const retentionControls = {
    maxAgeHoursInput,
    minKeepCountInput,
    unlimitedInput,
  };

  const applyRelayKeyDisplay = (): void => {
    const warnings = pairStatus?.transport_warnings ?? [];
    transportNote.textContent = warnings.join(" ");
    transportNote.hidden = warnings.length === 0;
    const paired = pairStatus?.paired_at !== null && pairStatus?.paired_at !== undefined;
    relayKeyNote.textContent = paired && pairStatus?.relay_key_configured === true
      ? RELAY_KEY_CONFIGURED_NOTE
      : RELAY_KEY_PENDING_NOTE;
  };

  const applyRelayProtocolDisplay = (): void => {
    const state = pairStatus?.relay_pairing_state ?? "none";
    const version = pairStatus?.relay_protocol_version ?? 1;
    const parts: string[] = [];
    if (pairStatus?.relay_key_configured === true) {
      parts.push(
        version >= 2
          ? "Relay protocol 2: separate keys per direction; domesti-bot keeps only a verifier of the key My Tracks sends."
          : "Relay protocol 1: one shared key for both directions. Pair again with a current My Tracks to upgrade.",
      );
    }
    if (state === "activating") {
      parts.push(
        "Activation sent but not confirmed by My Tracks yet. The previous pairing still works; use Check activation.",
      );
    } else if (state === "staged" || state === "probing") {
      parts.push("A new pairing is staged and being verified.");
    }
    const previousUntil = pairStatus?.relay_previous_key_expires_at ?? null;
    if (previousUntil !== null) {
      parts.push(`The previous key is still accepted until ${new Date(previousUntil * 1000).toLocaleTimeString()}.`);
    }
    relayProtocolNote.textContent = parts.join(" ");
    relayProtocolNote.hidden = parts.length === 0;
    requireV2Input.checked = pairStatus?.require_relay_protocol_2 === true;
    requireV2Input.disabled = storedConnection === null;
    checkActivationBtn.hidden = state !== "activating";
    revokePreviousBtn.hidden = previousUntil === null;
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
      const [nextPairStatus, nextConnection, monitoring] = await Promise.all([
        api.fetchMyTracksPairStatus(),
        api.fetchMyTracksSettings(),
        api.fetchMyTracksLocationMonitoring().catch(() => null),
      ]);
      pairStatus = nextPairStatus;
      storedConnection = nextConnection;
      approachRequestIntervalS = monitoring?.approach_request_interval_s ?? null;
      if (pairStatus?.location_history_retention) {
        savedRetention = { ...pairStatus.location_history_retention };
        applyRetentionToForm(pairStatus.location_history_retention, retentionControls);
      }
      renderPairStatus(status, pairStatus, approachRequestIntervalS);
      updatePairButtonLabel(pairBtn, pairStatus);
      applyRelayKeyDisplay();
      applyRelayProtocolDisplay();
      syncRetentionSaveState();
      syncResetButtonState();
    } catch (err) {
      setSettingsDialogStatus(
        status,
        `Could not load pairing status: ${formatError(err)}`,
        ToastVariant.Error,
      );
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
        setSettingsDialogStatus(status, message, ToastVariant.Error);
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
        renderPairStatus(status, pairStatus, approachRequestIntervalS);
        updatePairButtonLabel(pairBtn, pairStatus);
        applyRelayKeyDisplay();
        applyRelayProtocolDisplay();
        syncRetentionSaveState();
        syncResetButtonState();
        if (pairStatus.relay_pairing_state === "activating") {
          showInfoToast("Pairing staged; My Tracks has not confirmed the activation yet.");
        } else {
          showSuccessToast(rePair ? "My Tracks re-pairing complete." : "My Tracks pairing complete.");
        }
      } catch (err) {
        const message = formatError(err);
        setSettingsDialogStatus(status, message, ToastVariant.Error);
        showErrorToast(message);
        await refreshStatus();
      }
    })();
  });

  requireV2Input.addEventListener("change", () => {
    // One request at a time: a second click while the first PATCH is pending cannot race it.
    requireV2Input.disabled = true;
    void api
      .patchMyTracksRelayProtocol({ require_protocol_2: requireV2Input.checked })
      .then((next) => {
        pairStatus = next;
        applyRelayProtocolDisplay();
        showSuccessToast(
          next.require_relay_protocol_2 ? "Relay protocol 2 is now required." : "Relay protocol 2 is no longer required.",
        );
      })
      .catch((err: unknown) => {
        // Back to what the server last confirmed, not an inversion of whatever the box shows now.
        requireV2Input.checked = pairStatus?.require_relay_protocol_2 === true;
        showErrorToast(formatError(err));
      })
      .finally(() => {
        requireV2Input.disabled = storedConnection === null;
      });
  });

  checkActivationBtn.addEventListener("click", () => {
    void (async () => {
      const connection = options.readConnectionSettings();
      const password = await promptPairPassword(connection.username, true);
      if (password === null) {
        return;
      }
      try {
        const outcome = await api.postMyTracksPairReconcile({ password, username: connection.username });
        if (outcome.status !== null) {
          pairStatus = outcome.status;
        }
        const messages: Record<string, string> = {
          aborted: "My Tracks never activated the new keys; the previous pairing is unchanged.",
          none: "There is no pairing waiting for activation.",
          promoted: "My Tracks confirmed the activation; the new keys are active.",
          unconfirmed: "My Tracks still has not confirmed the activation.",
        };
        (outcome.result === "promoted" ? showSuccessToast : showInfoToast)(
          messages[outcome.result] ?? "Activation checked.",
        );
        await refreshStatus();
      } catch (err) {
        const message = formatError(err);
        setSettingsDialogStatus(status, message, ToastVariant.Error);
        showErrorToast(message);
      }
    })();
  });

  revokePreviousBtn.addEventListener("click", () => {
    // Disabled for the whole confirm-and-request sequence so a second click cannot open another confirmation.
    revokePreviousBtn.disabled = true;
    void (async () => {
      try {
        const confirmed = await confirmAction({
          title: "Revoke the previous key?",
          message:
            "My Tracks requests signed with the previous relay key are rejected immediately instead of after the " +
            "short grace period. Use this if the previous key may have been exposed.",
          confirmLabel: "Revoke",
          variant: ConfirmButtonVariant.Danger,
        });
        if (!confirmed) {
          return;
        }
        pairStatus = await api.postMyTracksRevokePreviousKey();
        applyRelayProtocolDisplay();
        showSuccessToast("The previous relay key was revoked.");
      } catch (err) {
        showErrorToast(formatError(err));
      } finally {
        // The display above hides the button after a successful revoke; either way it is usable again.
        revokePreviousBtn.disabled = false;
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
        setSettingsDialogStatus(status, message, ToastVariant.Error);
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
      variant: ConfirmButtonVariant.Danger,
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
          savedRetention = null;
          renderPairStatus(status, pairStatus, approachRequestIntervalS);
          updatePairButtonLabel(pairBtn, pairStatus);
          applyRelayKeyDisplay();
          applyRelayProtocolDisplay();
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
          setSettingsDialogStatus(status, message, ToastVariant.Error);
          showErrorToast(message);
        });
    });
  });

  await refreshStatus();
}
