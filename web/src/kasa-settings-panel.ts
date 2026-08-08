import { KasaCredentialsSource, type KasaPirRange } from "./closed-sets.js";
// Kasa/Tapo KLAP account credentials + motion (PIR) tuning for the Settings hub.

import { api, HttpError } from "./api.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  KasaCredentialsSettingsOut,
  KasaDeviceSettingsOut,
  KasaMotionTuningOut,
  KasaMotionTuningSetIn,
} from "./types.js";

export const KASA_MOTION_SETTINGS_APPLY_LABEL = "Apply motion tuning";
export const KASA_MOTION_SETTINGS_APPLY_UNCONFIRMED =
  "Motion settings were sent, but the switch did not confirm all values. Refresh and retry.";
export const KASA_MOTION_SETTINGS_LEGEND = "Motion (PIR) tuning";
export const KASA_MOTION_SETTINGS_NO_DEVICES =
  "No Kasa switches with PIR/motion were discovered. KS200M-class wall switches appear here after discovery.";
export const KASA_MOTION_SETTINGS_REFRESH_LABEL = "Refresh sensors";
export const KASA_MOTION_SETTINGS_TARGET_DEVICE_LABEL = "Target device";

function appendKasaIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Newer Kasa/Tapo devices use KLAP encryption and need your TP-Link account email and password for LAN control. Credentials are stored encrypted in the discovery database on this server.";
  parent.append(intro);
}

function appendMotionIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Configure PIR range / threshold and ambient light gating on motion-capable wall switches (for example KS200M). Triggered state is a polled ADC snapshot — short motion can be missed between refreshes.";
  parent.append(intro);
}

function createDeviceSelect(): {
  emptyHint: HTMLParagraphElement;
  label: HTMLLabelElement;
  select: HTMLSelectElement;
} {
  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = KASA_MOTION_SETTINGS_TARGET_DEVICE_LABEL;
  const select = document.createElement("select");
  select.name = "kasa_motion_device";
  select.required = false;
  label.append(labelText, select);

  const emptyHint = document.createElement("p");
  emptyHint.className = "settings-dialog-lead";
  emptyHint.textContent = KASA_MOTION_SETTINGS_NO_DEVICES;
  emptyHint.hidden = true;

  return { emptyHint, label, select };
}

function selectedValue(select: HTMLSelectElement): string | null {
  const value = select.value.trim();
  return value === "" ? null : value;
}

export async function mountKasaSettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "kasa-settings-form";
  form.noValidate = true;

  appendKasaIntro(form);

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const emailLabel = document.createElement("label");
  emailLabel.className = "settings-dialog-field";
  const emailText = document.createElement("span");
  emailText.textContent = "Account email";
  const emailInput = document.createElement("input");
  emailInput.type = "email";
  emailInput.name = "username";
  emailInput.autocomplete = "username";
  emailInput.required = true;
  emailInput.placeholder = "you@example.com";
  emailLabel.append(emailText, emailInput);

  const passwordLabel = document.createElement("label");
  passwordLabel.className = "settings-dialog-field";
  const passwordText = document.createElement("span");
  passwordText.textContent = "Password";
  const passwordRow = createSecretInputRow({
    autocomplete: "current-password",
    required: true,
  });
  const passwordInput = passwordRow.input;
  passwordInput.name = "password";
  passwordInput.placeholder = "Account password";
  let storedPassword: string | null = null;
  let passwordRevealed = false;
  const setPasswordRevealed = (revealed: boolean): void => {
    passwordRevealed = revealed;
    if (revealed && !passwordInput.value && storedPassword) {
      passwordInput.value = storedPassword;
    }
    passwordRow.setRevealed(revealed);
  };
  setPasswordRevealed(false);
  passwordLabel.append(passwordText, passwordRow.row);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Test";
  testBtn.disabled = true;
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored credentials";
  actions.append(saveBtn, testBtn, clearBtn);

  const credsSection = document.createElement("fieldset");
  credsSection.className = "settings-dialog-fieldset";
  const credsLegend = document.createElement("legend");
  credsLegend.textContent = "KLAP credentials";
  credsSection.append(credsLegend, status, emailLabel, passwordLabel, actions);

  const motionSection = document.createElement("fieldset");
  motionSection.className = "settings-dialog-fieldset ep1-calibration-section";
  const motionLegend = document.createElement("legend");
  motionLegend.textContent = KASA_MOTION_SETTINGS_LEGEND;
  motionSection.append(motionLegend);
  appendMotionIntro(motionSection);

  const deviceTarget = createDeviceSelect();
  motionSection.append(deviceTarget.label, deviceTarget.emptyHint);

  const motionStatus = document.createElement("p");
  motionStatus.className = "settings-dialog-status";
  motionStatus.hidden = true;

  const pirEnabledLabel = document.createElement("label");
  pirEnabledLabel.className = "settings-dialog-field settings-dialog-checkbox";
  const pirEnabledInput = document.createElement("input");
  pirEnabledInput.type = "checkbox";
  pirEnabledInput.name = "pir_enabled";
  const pirEnabledText = document.createElement("span");
  pirEnabledText.textContent = "PIR enabled";
  pirEnabledLabel.append(pirEnabledInput, pirEnabledText);

  const pirRangeLabel = document.createElement("label");
  pirRangeLabel.className = "settings-dialog-field";
  const pirRangeText = document.createElement("span");
  pirRangeText.textContent = "PIR range";
  const pirRangeSelect = document.createElement("select");
  pirRangeSelect.name = "pir_range";
  pirRangeLabel.append(pirRangeText, pirRangeSelect);

  const pirThresholdLabel = document.createElement("label");
  pirThresholdLabel.className = "settings-dialog-field";
  const pirThresholdText = document.createElement("span");
  pirThresholdText.textContent = "PIR threshold (0–100)";
  const pirThresholdInput = document.createElement("input");
  pirThresholdInput.type = "number";
  pirThresholdInput.name = "pir_threshold";
  pirThresholdInput.min = "0";
  pirThresholdInput.max = "100";
  pirThresholdInput.step = "1";
  pirThresholdLabel.append(pirThresholdText, pirThresholdInput);

  const ambientEnabledLabel = document.createElement("label");
  ambientEnabledLabel.className = "settings-dialog-field settings-dialog-checkbox";
  const ambientEnabledInput = document.createElement("input");
  ambientEnabledInput.type = "checkbox";
  ambientEnabledInput.name = "ambient_light_enabled";
  const ambientEnabledText = document.createElement("span");
  ambientEnabledText.textContent = "Ambient light enabled";
  ambientEnabledLabel.append(ambientEnabledInput, ambientEnabledText);

  const knobsStack = document.createElement("div");
  knobsStack.className = "ep1-calibration-offsets";
  knobsStack.append(
    pirEnabledLabel,
    pirRangeLabel,
    pirThresholdLabel,
    ambientEnabledLabel,
  );

  const sensors = document.createElement("dl");
  sensors.className = "settings-dialog-lead kasa-motion-sensors";
  const triggeredDt = document.createElement("dt");
  triggeredDt.textContent = "PIR triggered";
  const triggeredDd = document.createElement("dd");
  triggeredDd.textContent = "—";
  const percentDt = document.createElement("dt");
  percentDt.textContent = "PIR percent";
  const percentDd = document.createElement("dd");
  percentDd.textContent = "—";
  const ambientDt = document.createElement("dt");
  ambientDt.textContent = "Ambient light";
  const ambientDd = document.createElement("dd");
  ambientDd.textContent = "—";
  sensors.append(
    triggeredDt,
    triggeredDd,
    percentDt,
    percentDd,
    ambientDt,
    ambientDd,
  );

  const motionActions = document.createElement("div");
  motionActions.className = "settings-dialog-actions";
  const applyMotionBtn = document.createElement("button");
  applyMotionBtn.type = "button";
  applyMotionBtn.className = "btn";
  applyMotionBtn.textContent = KASA_MOTION_SETTINGS_APPLY_LABEL;
  const refreshMotionBtn = document.createElement("button");
  refreshMotionBtn.type = "button";
  refreshMotionBtn.className = "btn btn-secondary";
  refreshMotionBtn.textContent = KASA_MOTION_SETTINGS_REFRESH_LABEL;
  motionActions.append(applyMotionBtn, refreshMotionBtn);

  motionSection.append(motionStatus, knobsStack, sensors, motionActions);
  form.append(credsSection, motionSection);
  container.append(form);

  let settingsConfigured = false;
  let devices: KasaDeviceSettingsOut[] = [];
  let deviceLoadGeneration = 0;
  let deviceLoading = false;
  let baseline: KasaMotionTuningOut | null = null;

  const syncTestEnabled = (): void => {
    const formReady =
      emailInput.value.trim() !== "" && passwordInput.value !== "";
    testBtn.disabled = !(formReady || settingsConfigured);
  };

  const showStatusMessage = (message: string): void => {
    status.textContent = message;
    status.hidden = false;
  };

  const hideStatus = (): void => {
    status.textContent = "";
    status.hidden = true;
  };

  const showMotionStatus = (message: string): void => {
    motionStatus.textContent = message;
    motionStatus.hidden = false;
  };

  const hideMotionStatus = (): void => {
    motionStatus.textContent = "";
    motionStatus.hidden = true;
  };

  const syncMotionControls = (): void => {
    const hasDevices = devices.length > 0;
    const selected = selectedValue(deviceTarget.select);
    deviceTarget.emptyHint.hidden = hasDevices;
    deviceTarget.select.disabled = !hasDevices || deviceLoading;
    motionSection.hidden = false;
    applyMotionBtn.disabled =
      deviceLoading || !hasDevices || selected == null || baseline == null;
    refreshMotionBtn.disabled =
      deviceLoading || !hasDevices || selected == null;
    const ambientReady = baseline?.ambient_available === true;
    ambientEnabledLabel.hidden = !ambientReady && baseline != null;
    ambientEnabledInput.disabled = !ambientReady || deviceLoading;
  };

  const applyMotionSnapshot = (snap: KasaMotionTuningOut | null): void => {
    baseline = snap;
    if (snap == null) {
      pirEnabledInput.checked = false;
      pirRangeSelect.replaceChildren();
      pirThresholdInput.value = "";
      ambientEnabledInput.checked = false;
      triggeredDd.textContent = "—";
      percentDd.textContent = "—";
      ambientDd.textContent = "—";
      syncMotionControls();
      return;
    }
    pirEnabledInput.checked = snap.pir_enabled;
    pirRangeSelect.replaceChildren();
    for (const choice of snap.pir_range_choices) {
      const option = document.createElement("option");
      option.value = choice;
      option.textContent = choice;
      if (choice === snap.pir_range) {
        option.selected = true;
      }
      pirRangeSelect.append(option);
    }
    pirThresholdInput.value = String(snap.pir_threshold);
    ambientEnabledInput.checked = snap.ambient_light_enabled === true;
    triggeredDd.textContent = snap.pir_triggered ? "yes" : "no";
    percentDd.textContent =
      snap.pir_percent == null ? "—" : snap.pir_percent.toFixed(2);
    ambientDd.textContent =
      snap.ambient_light == null ? "—" : `${String(snap.ambient_light)}%`;
    syncMotionControls();
  };

  const loadMotionPanel = async (): Promise<void> => {
    const deviceId = selectedValue(deviceTarget.select);
    const generation = ++deviceLoadGeneration;
    deviceLoading = true;
    applyMotionSnapshot(null);
    syncMotionControls();
    if (deviceId == null) {
      if (generation === deviceLoadGeneration) {
        deviceLoading = false;
        syncMotionControls();
      }
      return;
    }
    try {
      const snap = await api.fetchKasaMotionTuning(deviceId);
      if (
        generation !== deviceLoadGeneration ||
        selectedValue(deviceTarget.select) !== deviceId
      ) {
        return;
      }
      applyMotionSnapshot(snap);
      hideMotionStatus();
    } catch (err) {
      if (
        generation !== deviceLoadGeneration ||
        selectedValue(deviceTarget.select) !== deviceId
      ) {
        return;
      }
      applyMotionSnapshot(null);
      showMotionStatus(
        err instanceof HttpError
          ? err.detail || err.message
          : "Could not load motion tuning.",
      );
    } finally {
      if (generation === deviceLoadGeneration) {
        deviceLoading = false;
        syncMotionControls();
      }
    }
  };

  const refreshMotionDevices = async (): Promise<void> => {
    try {
      const previous = selectedValue(deviceTarget.select);
      const out = await api.fetchKasaMotionDevices();
      devices = out.devices;
      deviceTarget.select.replaceChildren();
      for (const row of devices) {
        const option = document.createElement("option");
        option.value = row.device_id;
        option.textContent = row.display_label;
        deviceTarget.select.append(option);
      }
      if (devices.length === 0) {
        deviceTarget.select.value = "";
      } else if (
        previous != null &&
        devices.some((row) => row.device_id === previous)
      ) {
        deviceTarget.select.value = previous;
      } else {
        const first = devices[0];
        if (first != null) {
          deviceTarget.select.value = first.device_id;
        }
      }
      syncMotionControls();
      await loadMotionPanel();
    } catch (err) {
      devices = [];
      deviceTarget.select.replaceChildren();
      applyMotionSnapshot(null);
      syncMotionControls();
      showMotionStatus(
        err instanceof HttpError
          ? err.detail || err.message
          : "Could not load motion-capable Kasa devices.",
      );
    }
  };

  const applyFieldsFromSettings = (s: KasaCredentialsSettingsOut): void => {
    settingsConfigured = s.configured;
    storedPassword = s.stored_password;
    if (s.stored_username) {
      emailInput.value = s.stored_username;
    }
    if (storedPassword) {
      passwordInput.value = storedPassword;
      passwordInput.required = false;
      if (!passwordRevealed) {
        passwordInput.type = "password";
      }
    } else {
      passwordInput.value = "";
      passwordInput.required = true;
    }
    passwordInput.placeholder = storedPassword ? "" : "Account password";
    syncTestEnabled();
  };

  const updateStatusHint = (s: KasaCredentialsSettingsOut): void => {
    if (!s.secrets_key_configured) {
      showStatusMessage(
        "Add domesti_secrets_key to domesti-bot.config.json (see domesti-bot.config.json.example) or set DOMESTI_BOT_SECRETS_KEY before saving to the database.",
      );
      return;
    }
    if (s.skipped_auth_hosts.length > 0 && s.configured) {
      const sample = s.skipped_auth_hosts.slice(0, 3).join(", ");
      const more =
        s.skipped_auth_hosts.length > 3
          ? `, … (+${String(s.skipped_auth_hosts.length - 3)} more)`
          : "";
      const envNote =
        s.source === KasaCredentialsSource.Env
          ? " KASA_USERNAME / KASA_PASSWORD override the database."
          : "";
      showStatusMessage(
        `${String(s.skipped_auth_hosts.length)} device(s) failed KLAP auth on the last discovery (${sample}${more}). Check the account email/password.${envNote}`,
      );
      return;
    }
    if (s.source === KasaCredentialsSource.Env) {
      showStatusMessage(
        "KASA_USERNAME / KASA_PASSWORD override the database until you remove them.",
      );
      return;
    }
    if (s.hosts_requiring_klap_auth.length > 0) {
      const sample = s.hosts_requiring_klap_auth.slice(0, 3).join(", ");
      const more =
        s.hosts_requiring_klap_auth.length > 3
          ? `, … (+${String(s.hosts_requiring_klap_auth.length - 3)} more)`
          : "";
      if (s.configured) {
        showStatusMessage(
          `${String(s.hosts_requiring_klap_auth.length)} device(s) use KLAP account auth (${sample}${more}); other Kasa devices stay anonymous on the LAN.`,
        );
      } else {
        showStatusMessage(
          `${String(s.hosts_requiring_klap_auth.length)} device(s) need account credentials and are ignored until you save them (${sample}${more}).`,
        );
      }
      return;
    }
    hideStatus();
  };

  const refreshStatus = async (): Promise<void> => {
    try {
      const s = await api.fetchKasaCredentialsSettings();
      applyFieldsFromSettings(s);
      updateStatusHint(s);
    } catch (err) {
      showStatusMessage(
        err instanceof HttpError ? err.detail : "Could not load credential status.",
      );
    }
  };

  const saveCredentials = (): void => {
    void (async () => {
      const username = emailInput.value.trim();
      const password = passwordInput.value;
      if (!username) {
        showStatusMessage("Enter the Kasa/Tapo account email before saving.");
        return;
      }
      if (!password) {
        showStatusMessage("Enter the account password before saving.");
        return;
      }
      saveBtn.disabled = true;
      try {
        const out = await api.putKasaCredentials(username, password);
        showSuccessToast("Kasa credentials saved.");
        setPasswordRevealed(false);
        if (out.restart_required) {
          showStatusMessage(
            "Credentials saved. Restart domesti-bot (or remove KASA_USERNAME / KASA_PASSWORD) so devices use them.",
          );
        } else {
          await options.onDevicesChanged?.();
          await refreshMotionDevices();
        }
        try {
          const s = await api.fetchKasaCredentialsSettings();
          applyFieldsFromSettings(s);
          if (!out.restart_required) {
            updateStatusHint(s);
          }
        } catch {
          // Save already succeeded; a status-refresh failure is not a save failure.
        }
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Save failed.",
        );
      } finally {
        saveBtn.disabled = false;
      }
    })();
  };
  saveBtn.addEventListener("click", saveCredentials);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveCredentials();
  });
  emailInput.addEventListener("input", () => {
    syncTestEnabled();
  });
  passwordInput.addEventListener("input", () => {
    syncTestEnabled();
  });

  testBtn.addEventListener("click", () => {
    void (async () => {
      const username = emailInput.value.trim();
      const password = passwordInput.value;
      const formReady = username !== "" && password !== "";
      testBtn.disabled = true;
      showStatusMessage("Testing credentials…");
      try {
        const result = await api.testKasaCredentials(
          formReady ? { username, password } : {},
        );
        showStatusMessage(result.detail);
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Test failed.",
        );
      } finally {
        syncTestEnabled();
      }
    })();
  });

  clearBtn.addEventListener("click", () => {
    void (async () => {
      try {
        await api.clearKasaCredentials();
        emailInput.value = "";
        storedPassword = null;
        settingsConfigured = false;
        passwordInput.value = "";
        passwordInput.required = true;
        passwordInput.placeholder = "Account password";
        setPasswordRevealed(false);
        syncTestEnabled();
        showSuccessToast("Stored Kasa credentials cleared.");
        await refreshStatus();
        await options.onDevicesChanged?.();
        await refreshMotionDevices();
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Clear failed.",
        );
      }
    })();
  });

  deviceTarget.select.addEventListener("change", () => {
    void loadMotionPanel();
  });

  refreshMotionBtn.addEventListener("click", () => {
    void loadMotionPanel();
  });

  applyMotionBtn.addEventListener("click", () => {
    void (async () => {
      const deviceId = selectedValue(deviceTarget.select);
      if (deviceId == null || baseline == null) {
        return;
      }
      const body: KasaMotionTuningSetIn = {};
      const nextEnabled = pirEnabledInput.checked;
      if (nextEnabled !== baseline.pir_enabled) {
        body.pir_enabled = nextEnabled;
      }
      const thresholdRaw = pirThresholdInput.value.trim();
      const nextThreshold = Number(thresholdRaw);
      let thresholdChanged = false;
      if (
        thresholdRaw !== "" &&
        Number.isFinite(nextThreshold) &&
        nextThreshold !== baseline.pir_threshold
      ) {
        if (nextThreshold < 0 || nextThreshold > 100) {
          showMotionStatus("PIR threshold must be between 0 and 100.");
          return;
        }
        body.pir_threshold = Math.trunc(nextThreshold);
        thresholdChanged = true;
      }
      // Threshold writes force Custom on the device — omit a conflicting range.
      if (!thresholdChanged) {
        const nextRange = pirRangeSelect.value as KasaPirRange;
        if (nextRange !== baseline.pir_range) {
          body.pir_range = nextRange;
        }
      }
      if (baseline.ambient_available) {
        const nextAmbient = ambientEnabledInput.checked;
        if (nextAmbient !== (baseline.ambient_light_enabled === true)) {
          body.ambient_light_enabled = nextAmbient;
        }
      }
      if (
        body.pir_enabled == null &&
        body.pir_range == null &&
        body.pir_threshold == null &&
        body.ambient_light_enabled == null
      ) {
        showMotionStatus("No motion settings changed.");
        return;
      }
      applyMotionBtn.disabled = true;
      refreshMotionBtn.disabled = true;
      try {
        const snap = await api.putKasaMotionTuning(deviceId, body);
        if (selectedValue(deviceTarget.select) !== deviceId) {
          return;
        }
        applyMotionSnapshot(snap);
        if (snap.knobs_confirmed) {
          showSuccessToast("Motion tuning applied.");
          hideMotionStatus();
        } else {
          showErrorToast(KASA_MOTION_SETTINGS_APPLY_UNCONFIRMED);
          showMotionStatus(KASA_MOTION_SETTINGS_APPLY_UNCONFIRMED);
        }
      } catch (err) {
        showMotionStatus(
          err instanceof HttpError
            ? err.detail || err.message
            : "Apply motion tuning failed.",
        );
      } finally {
        syncMotionControls();
      }
    })();
  });

  await refreshStatus();
  await refreshMotionDevices();
}
