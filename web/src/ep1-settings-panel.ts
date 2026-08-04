// Everything Presence One settings: Noise PSK + climate/light calibration.

import { api, HttpError } from "./api.js";
import { Ep1CalibrationOffsetKind } from "./closed-sets.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showSuccessToast } from "./ui-toast.js";
import type {
  Ep1CalibrationOffsetFieldOut,
  Ep1CalibrationOut,
  Ep1DeviceSettingsOut,
  Ep1NoisePreSharedKeySettingsOut,
} from "./types.js";

const EP1_DOCS_HREF =
  "https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP";

export const EP1_SETTINGS_APPLY_OFFSETS_LABEL = "Apply offsets";
export const EP1_SETTINGS_CALIBRATION_LEGEND = "Calibration offsets";
export const EP1_SETTINGS_NO_DEVICES =
  "No EP1 devices discovered yet. Run discovery (or set EP1_HOSTS), then reopen Settings.";
export const EP1_SETTINGS_PSK_LEGEND = "Noise pre-shared key";
export const EP1_SETTINGS_PSK_OPTIONAL_HINT =
  "Optional for Homey / stock firmware (plaintext API). Required only when the device has ESPHome API encryption enabled.";
export const EP1_SETTINGS_SAVE_REQUIRES_PSK =
  "Enter a Noise pre-shared key (PSK) to save. For plaintext Homey firmware, leave this blank and use Test (or Clear stored key).";
export const EP1_SETTINGS_TARGET_DEVICE_LABEL = "Target device";
export const EP1_SETTINGS_TEST_TOOLTIP =
  "Probes the selected EP1 over the LAN with the PSK in the form (or the stored key / plaintext Homey). Does not change live discovery or device state.";

function appendEp1NoisePskIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  const link = document.createElement("a");
  link.href = EP1_DOCS_HREF;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "EP1 documentation";
  intro.append(
    document.createTextNode(
      "Optional ESPHome API encryption key — Noise pre-shared key (PSK) — from the ",
    ),
    link,
    document.createTextNode(
      ". Homey / pre-adoption firmware needs no PSK. When set, the key is stored encrypted in the discovery database on this server.",
    ),
  );
  parent.append(intro);

  const hint = document.createElement("p");
  hint.className = "settings-dialog-lead";
  hint.textContent = EP1_SETTINGS_PSK_OPTIONAL_HINT;
  parent.append(hint);
}

function appendCalibrationIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Climate and light calibration offsets are stored on the selected EP1 (ESPHome number entities). Apply writes only the fields you change.";
  parent.append(intro);
}

function createDeviceSelect(name: string): {
  emptyHint: HTMLParagraphElement;
  label: HTMLLabelElement;
  select: HTMLSelectElement;
} {
  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = EP1_SETTINGS_TARGET_DEVICE_LABEL;
  const select = document.createElement("select");
  select.name = name;
  select.required = false;
  label.append(labelText, select);

  const emptyHint = document.createElement("p");
  emptyHint.className = "settings-dialog-lead";
  emptyHint.textContent = EP1_SETTINGS_NO_DEVICES;
  emptyHint.hidden = true;

  return { emptyHint, label, select };
}

function createOffsetField(options: {
  kind: Ep1CalibrationOffsetKind;
  label: string;
}): {
  input: HTMLInputElement;
  reading: HTMLElement;
  root: HTMLLabelElement;
  applyField: (field: Ep1CalibrationOffsetFieldOut | null) => void;
  changedValue: () => number | null;
} {
  const root = document.createElement("label");
  root.className = "settings-dialog-field ep1-offset-field";
  const labelText = document.createElement("span");
  labelText.textContent = options.label;
  const controls = document.createElement("div");
  controls.className = "ep1-offset-field-controls";
  const input = document.createElement("input");
  input.type = "number";
  input.name = `${options.kind}_offset`;
  input.autocomplete = "off";
  const reading = document.createElement("span");
  reading.className = "ep1-offset-reading";
  reading.hidden = true;
  controls.append(input, reading);
  root.append(labelText, controls);

  let baseline: number | null = null;

  const applyField = (field: Ep1CalibrationOffsetFieldOut | null): void => {
    if (field == null || !field.available) {
      input.disabled = true;
      input.value = "";
      baseline = null;
      reading.hidden = true;
      reading.textContent = "";
      return;
    }
    input.disabled = false;
    if (field.min_value != null) {
      input.min = String(field.min_value);
    }
    if (field.max_value != null) {
      input.max = String(field.max_value);
    }
    if (field.step != null) {
      input.step = String(field.step);
    }
    baseline = field.value;
    input.value = field.value == null ? "" : String(field.value);
    if (field.reading == null) {
      reading.hidden = true;
      reading.textContent = "";
    } else {
      const unit = field.unit ? ` ${field.unit}` : "";
      reading.hidden = false;
      reading.textContent = `Live: ${field.reading.toFixed(2)}${unit}`;
    }
  };

  const changedValue = (): number | null => {
    if (input.disabled) {
      return null;
    }
    const raw = input.value.trim();
    if (raw === "") {
      return null;
    }
    const next = Number(raw);
    if (!Number.isFinite(next)) {
      return null;
    }
    if (baseline != null && next === baseline) {
      return null;
    }
    return next;
  };

  return { applyField, changedValue, input, reading, root };
}

function fillDeviceSelect(
  select: HTMLSelectElement,
  rows: readonly Ep1DeviceSettingsOut[],
  previous: string | null,
): void {
  select.replaceChildren();
  const placeholder = document.createElement("option");
  placeholder.value = "";
  placeholder.textContent = hasDevicesLabel(rows.length);
  select.append(placeholder);
  for (const row of rows) {
    const option = document.createElement("option");
    option.value = row.device_id;
    option.textContent = row.display_label;
    select.append(option);
  }
  if (previous != null && rows.some((row) => row.device_id === previous)) {
    select.value = previous;
  } else if (rows.length === 1) {
    const only = rows[0];
    if (only != null) {
      select.value = only.device_id;
    }
  }
}

function hasDevicesLabel(count: number): string {
  return count === 0 ? "No devices available" : "Select a device…";
}

function selectedValue(select: HTMLSelectElement): string | null {
  const value = select.value.trim();
  return value === "" ? null : value;
}

export async function mountEp1SettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "ep1-settings-form";
  form.noValidate = true;

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const pskSection = document.createElement("fieldset");
  pskSection.className = "settings-dialog-fieldset ep1-psk-section";
  const pskLegend = document.createElement("legend");
  pskLegend.textContent = EP1_SETTINGS_PSK_LEGEND;
  pskSection.append(pskLegend);
  appendEp1NoisePskIntro(pskSection);

  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = "Noise pre-shared key (PSK)";
  const secretRow = createSecretInputRow({
    autocomplete: "off",
    required: false,
  });
  const input = secretRow.input;
  input.name = "noise_psk";
  let storedPsk: string | null = null;
  const setRevealed = (next: boolean): void => {
    if (next && !input.value && storedPsk) {
      input.value = storedPsk;
    }
    secretRow.setRevealed(next);
  };
  setRevealed(false);
  label.append(labelText, secretRow.row);

  const pskTarget = createDeviceSelect("psk_device_id");
  const pskActions = document.createElement("div");
  pskActions.className = "settings-dialog-actions";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Test";
  testBtn.title = EP1_SETTINGS_TEST_TOOLTIP;
  testBtn.disabled = true;
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored key";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  pskActions.append(testBtn, clearBtn, saveBtn);
  pskSection.append(
    label,
    pskTarget.label,
    pskTarget.emptyHint,
    pskActions,
  );

  const calibrationSection = document.createElement("fieldset");
  calibrationSection.className =
    "settings-dialog-fieldset ep1-calibration-section";
  const calibrationLegend = document.createElement("legend");
  calibrationLegend.textContent = EP1_SETTINGS_CALIBRATION_LEGEND;
  calibrationSection.append(calibrationLegend);
  appendCalibrationIntro(calibrationSection);

  const calibrationTarget = createDeviceSelect("calibration_device_id");
  const humidityField = createOffsetField({
    kind: Ep1CalibrationOffsetKind.Humidity,
    label: "Humidity offset (%)",
  });
  const illuminanceField = createOffsetField({
    kind: Ep1CalibrationOffsetKind.Illuminance,
    label: "Illuminance offset (lx)",
  });
  const temperatureField = createOffsetField({
    kind: Ep1CalibrationOffsetKind.Temperature,
    label: "Temperature offset (°C)",
  });
  const offsetsStack = document.createElement("div");
  offsetsStack.className = "ep1-calibration-offsets";
  offsetsStack.append(
    temperatureField.root,
    humidityField.root,
    illuminanceField.root,
  );

  const calibrationActions = document.createElement("div");
  calibrationActions.className = "settings-dialog-actions";
  const applyOffsetsBtn = document.createElement("button");
  applyOffsetsBtn.type = "button";
  applyOffsetsBtn.className = "btn";
  applyOffsetsBtn.textContent = EP1_SETTINGS_APPLY_OFFSETS_LABEL;
  calibrationActions.append(applyOffsetsBtn);
  calibrationSection.append(
    calibrationTarget.label,
    calibrationTarget.emptyHint,
    offsetsStack,
    calibrationActions,
  );

  form.append(status, pskSection, calibrationSection);
  container.append(form);

  let devices: Ep1DeviceSettingsOut[] = [];
  let calibrationLoadGeneration = 0;
  let calibrationLoading = false;
  let pskTestGeneration = 0;

  const syncPskTargetControls = (): void => {
    const hasDevices = devices.length > 0;
    pskTarget.emptyHint.hidden = hasDevices;
    pskTarget.select.disabled = !hasDevices;
    testBtn.disabled = !hasDevices || selectedValue(pskTarget.select) == null;
  };

  const syncCalibrationTargetControls = (): void => {
    const hasDevices = devices.length > 0;
    calibrationTarget.emptyHint.hidden = hasDevices;
    calibrationTarget.select.disabled = !hasDevices;
    applyOffsetsBtn.disabled =
      calibrationLoading ||
      !hasDevices ||
      selectedValue(calibrationTarget.select) == null;
  };

  const applyCalibration = (snap: Ep1CalibrationOut | null): void => {
    humidityField.applyField(snap?.humidity ?? null);
    illuminanceField.applyField(snap?.illuminance ?? null);
    temperatureField.applyField(snap?.temperature ?? null);
  };

  const loadCalibrationForSelection = async (): Promise<void> => {
    const deviceId = selectedValue(calibrationTarget.select);
    const generation = ++calibrationLoadGeneration;
    calibrationLoading = true;
    applyCalibration(null);
    syncCalibrationTargetControls();
    if (deviceId == null) {
      if (generation === calibrationLoadGeneration) {
        calibrationLoading = false;
        syncCalibrationTargetControls();
      }
      return;
    }
    try {
      const snap = await api.fetchEp1Calibration(deviceId);
      if (
        generation !== calibrationLoadGeneration ||
        selectedValue(calibrationTarget.select) !== deviceId
      ) {
        return;
      }
      applyCalibration(snap);
      status.hidden = true;
    } catch (err) {
      if (
        generation !== calibrationLoadGeneration ||
        selectedValue(calibrationTarget.select) !== deviceId
      ) {
        return;
      }
      applyCalibration(null);
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    }
    if (generation === calibrationLoadGeneration) {
      calibrationLoading = false;
      syncCalibrationTargetControls();
    }
  };

  const fillDevices = (rows: Ep1DeviceSettingsOut[]): void => {
    const previousPsk = selectedValue(pskTarget.select);
    const previousCalibration = selectedValue(calibrationTarget.select);
    devices = rows;
    fillDeviceSelect(pskTarget.select, rows, previousPsk);
    fillDeviceSelect(calibrationTarget.select, rows, previousCalibration);
    syncPskTargetControls();
    syncCalibrationTargetControls();
  };

  const applyFromSettings = (s: Ep1NoisePreSharedKeySettingsOut): void => {
    storedPsk = s.stored_noise_psk;
    if (storedPsk) {
      input.value = storedPsk;
    } else {
      input.value = "";
    }
    input.required = false;
    setRevealed(false);
    status.hidden = true;
  };

  const showError = (message: string): void => {
    status.hidden = false;
    status.textContent = message;
    status.classList.add("settings-dialog-status-error");
  };

  try {
    applyFromSettings(await api.fetchEp1NoisePskSettings());
    fillDevices((await api.fetchEp1Devices()).devices);
    await loadCalibrationForSelection();
  } catch (err) {
    showError(err instanceof HttpError ? err.detail || err.message : String(err));
  }

  pskTarget.select.addEventListener("change", () => {
    pskTestGeneration += 1;
    syncPskTargetControls();
  });
  calibrationTarget.select.addEventListener("change", () => {
    void loadCalibrationForSelection();
  });

  saveBtn.addEventListener("click", async () => {
    const noisePsk = input.value.trim();
    if (!noisePsk) {
      showError(EP1_SETTINGS_SAVE_REQUIRES_PSK);
      return;
    }
    saveBtn.disabled = true;
    try {
      const out = await api.putEp1NoisePsk(noisePsk);
      applyFromSettings(await api.fetchEp1NoisePskSettings());
      showSuccessToast(
        out.restart_required
          ? "EP1 Noise pre-shared key saved — restart the server to apply."
          : "EP1 Noise pre-shared key saved.",
      );
      await options.onDevicesChanged?.();
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      saveBtn.disabled = false;
    }
  });

  applyOffsetsBtn.addEventListener("click", async () => {
    const deviceId = selectedValue(calibrationTarget.select);
    if (deviceId == null) {
      showError(EP1_SETTINGS_NO_DEVICES);
      return;
    }
    const body = {
      humidity_offset: humidityField.changedValue(),
      illuminance_offset: illuminanceField.changedValue(),
      temperature_offset: temperatureField.changedValue(),
    };
    if (
      body.humidity_offset == null &&
      body.illuminance_offset == null &&
      body.temperature_offset == null
    ) {
      showError("Change at least one offset before applying.");
      return;
    }
    applyOffsetsBtn.disabled = true;
    try {
      const snap = await api.putEp1Calibration(deviceId, body);
      if (selectedValue(calibrationTarget.select) !== deviceId) {
        return;
      }
      applyCalibration(snap);
      status.hidden = false;
      status.classList.remove("settings-dialog-status-error");
      status.textContent = `Offsets applied on ${snap.display_label}.`;
      showSuccessToast(`EP1 offsets saved on ${snap.display_label}.`);
    } catch (err) {
      if (selectedValue(calibrationTarget.select) !== deviceId) {
        return;
      }
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      if (selectedValue(calibrationTarget.select) === deviceId) {
        syncCalibrationTargetControls();
      }
    }
  });

  testBtn.addEventListener("click", async () => {
    const deviceId = selectedValue(pskTarget.select);
    if (deviceId == null) {
      showError(EP1_SETTINGS_NO_DEVICES);
      return;
    }
    const generation = ++pskTestGeneration;
    testBtn.disabled = true;
    try {
      const result = await api.testEp1NoisePsk({
        noise_psk: input.value.trim() || null,
        device_id: deviceId,
      });
      if (
        generation !== pskTestGeneration ||
        selectedValue(pskTarget.select) !== deviceId
      ) {
        return;
      }
      status.hidden = false;
      status.classList.toggle("settings-dialog-status-error", !result.ok);
      status.textContent = result.detail;
    } catch (err) {
      if (
        generation !== pskTestGeneration ||
        selectedValue(pskTarget.select) !== deviceId
      ) {
        return;
      }
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      if (generation === pskTestGeneration) {
        syncPskTargetControls();
      }
    }
  });

  clearBtn.addEventListener("click", async () => {
    clearBtn.disabled = true;
    try {
      await api.clearEp1NoisePsk();
      applyFromSettings(await api.fetchEp1NoisePskSettings());
      showSuccessToast("Stored EP1 Noise pre-shared key cleared.");
      await options.onDevicesChanged?.();
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      clearBtn.disabled = false;
    }
  });
}
