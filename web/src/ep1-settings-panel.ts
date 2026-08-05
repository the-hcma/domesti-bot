// Everything Presence One settings: Noise PSK + calibration + occupancy tuning.

import { api, HttpError } from "./api.js";
import {
  Ep1CalibrationOffsetKind,
  Ep1OccupancyTuningKind,
} from "./closed-sets.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  Ep1CalibrationOffsetFieldOut,
  Ep1CalibrationOut,
  Ep1DeviceSettingsOut,
  Ep1NoisePreSharedKeySettingsOut,
  Ep1OccupancyTuningFieldOut,
  Ep1OccupancyTuningOut,
  Ep1OccupancyTuningSetIn,
} from "./types.js";

const EP1_DOCS_HREF =
  "https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP";
const EP1_TUNE_DOCS_HREF =
  "https://docs.everythingsmart.io/s/products/doc/how-to-tune-your-ep1-sensor-eJwL48QXTH";

export const EP1_SETTINGS_APPLY_OCCUPANCY_LABEL = "Apply occupancy tuning";
export const EP1_SETTINGS_APPLY_OFFSETS_LABEL = "Apply offsets";
export const EP1_SETTINGS_CALIBRATION_LEGEND = "Calibration offsets";
export const EP1_SETTINGS_NO_DEVICES =
  "No EP1 devices discovered yet. Run discovery (or set EP1_HOSTS), then reopen Settings.";
export const EP1_SETTINGS_OCCUPANCY_APPLY_NOTE =
  "Changing min/max distance presses firmware Set Distance; changing trigger/sustain sensitivity presses Set Sensitivity. Latency and trigger distance apply immediately.";
export const EP1_SETTINGS_OCCUPANCY_LEGEND = "Occupancy tuning";
export const EP1_SETTINGS_OFFSET_OUT_OF_RANGE = (
  kindLabel: string,
  min: number | null,
  max: number | null,
  value: number,
): string => {
  if (min != null && max != null) {
    return `${kindLabel} must be between ${String(min)} and ${String(max)} (got ${String(value)}).`;
  }
  if (min != null) {
    return `${kindLabel} must be ≥ ${String(min)} (got ${String(value)}).`;
  }
  if (max != null) {
    return `${kindLabel} must be ≤ ${String(max)} (got ${String(value)}).`;
  }
  return `${kindLabel} is out of range (got ${String(value)}).`;
};
export const EP1_SETTINGS_PSK_LEGEND = "Noise pre-shared key";
export const EP1_SETTINGS_PSK_OPTIONAL_HINT =
  "Optional for Homey / stock firmware (plaintext API). Required only when the device has ESPHome API encryption enabled.";
export const EP1_SETTINGS_SAVE_REQUIRES_PSK =
  "Enter a Noise pre-shared key (PSK) to save. For plaintext Homey firmware, leave this blank and use Test (or Clear stored key).";
export const EP1_SETTINGS_STEP_MISALIGNED = (
  kindLabel: string,
  step: number,
  min: number,
  value: number,
): string =>
  `${kindLabel}: use a value in steps of ${String(step)} from ${String(min)}. Got ${String(value)}.`;
export const EP1_SETTINGS_TARGET_DEVICE_LABEL = "Target device";
export const EP1_SETTINGS_TEST_TOOLTIP =
  "Probes the selected EP1 over the LAN with the PSK in the form (or the stored key / plaintext Homey). Does not change live discovery or device state.";

function appendCalibrationIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Offsets are additive adjustments stored on the EP1 (not absolute targets). Stock firmware ranges: temperature −20…20 °C, humidity −50…50 %, illuminance −50…50 lx. Apply writes only the fields you change, then waits for the live reading to refresh.";
  parent.append(intro);
}

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

function appendOccupancyIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  const link = document.createElement("a");
  link.href = EP1_TUNE_DOCS_HREF;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "vendor tune guide";
  intro.append(
    document.createTextNode(
      "mmWave knobs for false-presence tuning (SEN0609). Domesti-bot mirrors the combined occupancy binary — it does not invent presence. See the ",
    ),
    link,
    document.createTextNode(". "),
    document.createTextNode(EP1_SETTINGS_OCCUPANCY_APPLY_NOTE),
  );
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

function createKnobField(options: {
  kind: Ep1OccupancyTuningKind;
  label: string;
}): {
  input: HTMLInputElement;
  rangeHint: HTMLElement;
  root: HTMLLabelElement;
  applyField: (field: Ep1OccupancyTuningFieldOut | null) => void;
  changedValue: () => number | null;
  validateChanged: () => string | null;
} {
  const root = document.createElement("label");
  root.className = "settings-dialog-field ep1-offset-field";
  const labelText = document.createElement("span");
  labelText.textContent = options.label;
  const controls = document.createElement("div");
  controls.className = "ep1-offset-field-controls";
  const input = document.createElement("input");
  input.type = "number";
  input.name = options.kind;
  input.autocomplete = "off";
  controls.append(input);
  const rangeHint = document.createElement("span");
  rangeHint.className = "ep1-offset-range-hint";
  rangeHint.hidden = true;
  root.append(labelText, controls, rangeHint);

  let baseline: number | null = null;
  let maxValue: number | null = null;
  let minValue: number | null = null;
  let stepValue: number | null = null;

  const applyField = (field: Ep1OccupancyTuningFieldOut | null): void => {
    if (field == null || !field.available) {
      input.disabled = true;
      input.value = "";
      baseline = null;
      minValue = null;
      maxValue = null;
      stepValue = null;
      input.removeAttribute("min");
      input.removeAttribute("max");
      input.removeAttribute("step");
      rangeHint.hidden = true;
      rangeHint.textContent = "";
      return;
    }
    input.disabled = false;
    minValue = field.min_value;
    maxValue = field.max_value;
    stepValue = field.step;
    if (field.min_value != null) {
      input.min = String(field.min_value);
    } else {
      input.removeAttribute("min");
    }
    if (field.max_value != null) {
      input.max = String(field.max_value);
    } else {
      input.removeAttribute("max");
    }
    if (field.step != null) {
      input.step = String(field.step);
    } else {
      input.removeAttribute("step");
    }
    baseline = field.value;
    input.value = field.value == null ? "" : String(field.value);
    if (field.min_value != null && field.max_value != null) {
      const step =
        field.step != null ? ` · step ${String(field.step)}` : "";
      const unit = field.unit ? ` ${field.unit}` : "";
      rangeHint.hidden = false;
      rangeHint.textContent = `Range ${String(field.min_value)}…${String(field.max_value)}${unit}${step}`;
    } else if (field.min_value != null) {
      rangeHint.hidden = false;
      rangeHint.textContent = `Min ${String(field.min_value)}${field.unit ? ` ${field.unit}` : ""}`;
    } else if (field.max_value != null) {
      rangeHint.hidden = false;
      rangeHint.textContent = `Max ${String(field.max_value)}${field.unit ? ` ${field.unit}` : ""}`;
    } else {
      rangeHint.hidden = true;
      rangeHint.textContent = "";
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

  const validateChanged = (): string | null => {
    const next = changedValue();
    if (next == null) {
      return null;
    }
    if (minValue != null && next < minValue) {
      return EP1_SETTINGS_OFFSET_OUT_OF_RANGE(
        options.label,
        minValue,
        maxValue,
        next,
      );
    }
    if (maxValue != null && next > maxValue) {
      return EP1_SETTINGS_OFFSET_OUT_OF_RANGE(
        options.label,
        minValue,
        maxValue,
        next,
      );
    }
    if (
      stepValue != null &&
      stepValue > 0 &&
      minValue != null &&
      next !== minValue &&
      next !== maxValue
    ) {
      const steps = (next - minValue) / stepValue;
      if (Math.abs(steps - Math.round(steps)) > 1e-6) {
        return EP1_SETTINGS_STEP_MISALIGNED(
          options.label,
          stepValue,
          minValue,
          next,
        );
      }
    }
    return null;
  };

  return {
    applyField,
    changedValue,
    input,
    rangeHint,
    root,
    validateChanged,
  };
}

function createOffsetField(options: {
  kind: Ep1CalibrationOffsetKind;
  label: string;
}): {
  input: HTMLInputElement;
  reading: HTMLElement;
  rangeHint: HTMLElement;
  root: HTMLLabelElement;
  applyField: (field: Ep1CalibrationOffsetFieldOut | null) => void;
  changedValue: () => number | null;
  validateChanged: () => string | null;
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
  const rangeHint = document.createElement("span");
  rangeHint.className = "ep1-offset-range-hint";
  rangeHint.hidden = true;
  root.append(labelText, controls, rangeHint);

  let baseline: number | null = null;
  let maxValue: number | null = null;
  let minValue: number | null = null;

  const applyField = (field: Ep1CalibrationOffsetFieldOut | null): void => {
    if (field == null || !field.available) {
      input.disabled = true;
      input.value = "";
      baseline = null;
      minValue = null;
      maxValue = null;
      input.removeAttribute("min");
      input.removeAttribute("max");
      input.removeAttribute("step");
      reading.hidden = true;
      reading.textContent = "";
      rangeHint.hidden = true;
      rangeHint.textContent = "";
      return;
    }
    input.disabled = false;
    minValue = field.min_value;
    maxValue = field.max_value;
    if (field.min_value != null) {
      input.min = String(field.min_value);
    } else {
      input.removeAttribute("min");
    }
    if (field.max_value != null) {
      input.max = String(field.max_value);
    } else {
      input.removeAttribute("max");
    }
    if (field.step != null) {
      input.step = String(field.step);
    } else {
      input.removeAttribute("step");
    }
    baseline = field.value;
    input.value = field.value == null ? "" : String(field.value);
    if (field.min_value != null && field.max_value != null) {
      const step =
        field.step != null ? ` · step ${String(field.step)}` : "";
      rangeHint.hidden = false;
      rangeHint.textContent = `Range ${String(field.min_value)}…${String(field.max_value)}${step}`;
    } else if (field.min_value != null) {
      rangeHint.hidden = false;
      rangeHint.textContent = `Min ${String(field.min_value)}`;
    } else if (field.max_value != null) {
      rangeHint.hidden = false;
      rangeHint.textContent = `Max ${String(field.max_value)}`;
    } else {
      rangeHint.hidden = true;
      rangeHint.textContent = "";
    }
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

  const validateChanged = (): string | null => {
    const next = changedValue();
    if (next == null) {
      return null;
    }
    if (minValue != null && next < minValue) {
      return EP1_SETTINGS_OFFSET_OUT_OF_RANGE(
        options.label,
        minValue,
        maxValue,
        next,
      );
    }
    if (maxValue != null && next > maxValue) {
      return EP1_SETTINGS_OFFSET_OUT_OF_RANGE(
        options.label,
        minValue,
        maxValue,
        next,
      );
    }
    return null;
  };

  return {
    applyField,
    changedValue,
    input,
    rangeHint,
    reading,
    root,
    validateChanged,
  };
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

function formatOccupancyAppliedMessage(snap: Ep1OccupancyTuningOut): string {
  if (!snap.knobs_confirmed) {
    return (
      `Occupancy tuning was sent to ${snap.display_label}, but the device did not confirm ` +
      `the new values yet. Re-check Settings in a moment.`
    );
  }
  const parts = [`Occupancy tuning applied on ${snap.display_label}`];
  if (snap.distance_applied) {
    parts.push("Set Distance pressed");
  }
  if (snap.sensitivity_applied) {
    parts.push("Set Sensitivity pressed");
  }
  return `${parts.join(" — ")}.`;
}

function formatOffsetsAppliedMessage(
  snap: Ep1CalibrationOut,
  body: {
    humidity_offset: number | null;
    illuminance_offset: number | null;
    temperature_offset: number | null;
  },
): string {
  if (!snap.offsets_confirmed) {
    return (
      `Offsets were sent to ${snap.display_label}, but the device did not confirm ` +
      `the new values yet. Re-check calibration in a moment.`
    );
  }
  const parts = [`Offsets applied on ${snap.display_label}`];
  const readingBits: string[] = [];
  if (body.temperature_offset != null && snap.temperature.reading != null) {
    readingBits.push(`temp ${snap.temperature.reading.toFixed(2)} °C`);
  }
  if (body.humidity_offset != null && snap.humidity.reading != null) {
    readingBits.push(`humidity ${snap.humidity.reading.toFixed(2)}%`);
  }
  if (body.illuminance_offset != null && snap.illuminance.reading != null) {
    readingBits.push(`illuminance ${snap.illuminance.reading.toFixed(2)} lx`);
  }
  if (readingBits.length > 0) {
    if (snap.readings_refreshed) {
      parts.push(`live ${readingBits.join(", ")}`);
    } else {
      parts.push(
        `readings may still be catching up (${readingBits.join(", ")})`,
      );
    }
  }
  return `${parts.join(" — ")}.`;
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

  const deviceTarget = createDeviceSelect("device_id");

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
  pskSection.append(label, pskActions);

  const calibrationSection = document.createElement("fieldset");
  calibrationSection.className =
    "settings-dialog-fieldset ep1-calibration-section";
  const calibrationLegend = document.createElement("legend");
  calibrationLegend.textContent = EP1_SETTINGS_CALIBRATION_LEGEND;
  calibrationSection.append(calibrationLegend);
  appendCalibrationIntro(calibrationSection);

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
  calibrationSection.append(offsetsStack, calibrationActions);

  const occupancySection = document.createElement("fieldset");
  occupancySection.className =
    "settings-dialog-fieldset ep1-calibration-section";
  const occupancyLegend = document.createElement("legend");
  occupancyLegend.textContent = EP1_SETTINGS_OCCUPANCY_LEGEND;
  occupancySection.append(occupancyLegend);
  appendOccupancyIntro(occupancySection);

  const maxDistanceField = createKnobField({
    kind: Ep1OccupancyTuningKind.MaxDistance,
    label: "Max distance (m)",
  });
  const minDistanceField = createKnobField({
    kind: Ep1OccupancyTuningKind.MinDistance,
    label: "Min distance (m)",
  });
  const offLatencyField = createKnobField({
    kind: Ep1OccupancyTuningKind.OffLatency,
    label: "Off latency (s)",
  });
  const onLatencyField = createKnobField({
    kind: Ep1OccupancyTuningKind.OnLatency,
    label: "On latency (s)",
  });
  const sustainSensitivityField = createKnobField({
    kind: Ep1OccupancyTuningKind.SustainSensitivity,
    label: "Sustain sensitivity",
  });
  const triggerDistanceField = createKnobField({
    kind: Ep1OccupancyTuningKind.TriggerDistance,
    label: "Trigger distance (m)",
  });
  const triggerSensitivityField = createKnobField({
    kind: Ep1OccupancyTuningKind.TriggerSensitivity,
    label: "Trigger sensitivity",
  });
  const knobsStack = document.createElement("div");
  knobsStack.className = "ep1-calibration-offsets";
  knobsStack.append(
    minDistanceField.root,
    maxDistanceField.root,
    triggerDistanceField.root,
    triggerSensitivityField.root,
    sustainSensitivityField.root,
    onLatencyField.root,
    offLatencyField.root,
  );
  const occupancyKnobFields = [
    minDistanceField,
    maxDistanceField,
    triggerDistanceField,
    triggerSensitivityField,
    sustainSensitivityField,
    onLatencyField,
    offLatencyField,
  ];

  const occupancyActions = document.createElement("div");
  occupancyActions.className = "settings-dialog-actions";
  const applyOccupancyBtn = document.createElement("button");
  applyOccupancyBtn.type = "button";
  applyOccupancyBtn.className = "btn";
  applyOccupancyBtn.textContent = EP1_SETTINGS_APPLY_OCCUPANCY_LABEL;
  occupancyActions.append(applyOccupancyBtn);
  occupancySection.append(knobsStack, occupancyActions);

  form.append(
    status,
    deviceTarget.label,
    deviceTarget.emptyHint,
    pskSection,
    calibrationSection,
    occupancySection,
  );
  container.append(form);

  let devices: Ep1DeviceSettingsOut[] = [];
  let deviceLoadGeneration = 0;
  let deviceLoading = false;
  let pskTestGeneration = 0;

  const syncDeviceControls = (): void => {
    const hasDevices = devices.length > 0;
    const selected = selectedValue(deviceTarget.select);
    deviceTarget.emptyHint.hidden = hasDevices;
    deviceTarget.select.disabled = !hasDevices;
    testBtn.disabled = !hasDevices || selected == null;
    applyOffsetsBtn.disabled =
      deviceLoading || !hasDevices || selected == null;
    applyOccupancyBtn.disabled =
      deviceLoading || !hasDevices || selected == null;
  };

  const applyCalibration = (snap: Ep1CalibrationOut | null): void => {
    humidityField.applyField(snap?.humidity ?? null);
    illuminanceField.applyField(snap?.illuminance ?? null);
    temperatureField.applyField(snap?.temperature ?? null);
  };

  const applyOccupancy = (snap: Ep1OccupancyTuningOut | null): void => {
    maxDistanceField.applyField(snap?.max_distance ?? null);
    minDistanceField.applyField(snap?.min_distance ?? null);
    offLatencyField.applyField(snap?.off_latency ?? null);
    onLatencyField.applyField(snap?.on_latency ?? null);
    sustainSensitivityField.applyField(snap?.sustain_sensitivity ?? null);
    triggerDistanceField.applyField(snap?.trigger_distance ?? null);
    triggerSensitivityField.applyField(snap?.trigger_sensitivity ?? null);
  };

  const loadDevicePanels = async (): Promise<void> => {
    const deviceId = selectedValue(deviceTarget.select);
    const generation = ++deviceLoadGeneration;
    deviceLoading = true;
    applyCalibration(null);
    applyOccupancy(null);
    syncDeviceControls();
    if (deviceId == null) {
      if (generation === deviceLoadGeneration) {
        deviceLoading = false;
        syncDeviceControls();
      }
      return;
    }
    try {
      const [calibrationResult, occupancyResult] = await Promise.allSettled([
        api.fetchEp1Calibration(deviceId),
        api.fetchEp1OccupancyTuning(deviceId),
      ]);
      if (
        generation !== deviceLoadGeneration ||
        selectedValue(deviceTarget.select) !== deviceId
      ) {
        return;
      }
      applyCalibration(
        calibrationResult.status === "fulfilled" ? calibrationResult.value : null,
      );
      applyOccupancy(
        occupancyResult.status === "fulfilled" ? occupancyResult.value : null,
      );
      const failure =
        calibrationResult.status === "rejected"
          ? calibrationResult.reason
          : occupancyResult.status === "rejected"
            ? occupancyResult.reason
            : null;
      if (failure == null) {
        status.hidden = true;
      } else {
        showError(
          failure instanceof HttpError
            ? failure.detail || failure.message
            : String(failure),
        );
      }
    } finally {
      if (generation === deviceLoadGeneration) {
        deviceLoading = false;
        syncDeviceControls();
      }
    }
  };

  const fillDevices = (rows: Ep1DeviceSettingsOut[]): void => {
    const previous = selectedValue(deviceTarget.select);
    devices = rows;
    fillDeviceSelect(deviceTarget.select, rows, previous);
    syncDeviceControls();
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
    showErrorToast(message);
  };

  const showSuccess = (message: string): void => {
    status.hidden = false;
    status.classList.remove("settings-dialog-status-error");
    status.textContent = message;
    showSuccessToast(message);
  };

  try {
    applyFromSettings(await api.fetchEp1NoisePskSettings());
    fillDevices((await api.fetchEp1Devices()).devices);
    await loadDevicePanels();
  } catch (err) {
    showError(err instanceof HttpError ? err.detail || err.message : String(err));
  }

  deviceTarget.select.addEventListener("change", () => {
    pskTestGeneration += 1;
    void loadDevicePanels();
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
      showSuccess(
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
    const deviceId = selectedValue(deviceTarget.select);
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
    for (const field of [temperatureField, humidityField, illuminanceField]) {
      const rangeError = field.validateChanged();
      if (rangeError != null) {
        showError(rangeError);
        return;
      }
    }
    applyOffsetsBtn.disabled = true;
    try {
      const snap = await api.putEp1Calibration(deviceId, body);
      if (selectedValue(deviceTarget.select) !== deviceId) {
        return;
      }
      applyCalibration(snap);
      const message = formatOffsetsAppliedMessage(snap, body);
      if (!snap.offsets_confirmed) {
        showError(message);
      } else {
        showSuccess(message);
      }
    } catch (err) {
      if (selectedValue(deviceTarget.select) !== deviceId) {
        return;
      }
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      if (selectedValue(deviceTarget.select) === deviceId) {
        syncDeviceControls();
      }
    }
  });

  applyOccupancyBtn.addEventListener("click", async () => {
    const deviceId = selectedValue(deviceTarget.select);
    if (deviceId == null) {
      showError(EP1_SETTINGS_NO_DEVICES);
      return;
    }
    const body: Ep1OccupancyTuningSetIn = {
      max_distance: maxDistanceField.changedValue(),
      min_distance: minDistanceField.changedValue(),
      off_latency: offLatencyField.changedValue(),
      on_latency: onLatencyField.changedValue(),
      sustain_sensitivity: sustainSensitivityField.changedValue(),
      trigger_distance: triggerDistanceField.changedValue(),
      trigger_sensitivity: triggerSensitivityField.changedValue(),
    };
    if (
      body.max_distance == null &&
      body.min_distance == null &&
      body.off_latency == null &&
      body.on_latency == null &&
      body.sustain_sensitivity == null &&
      body.trigger_distance == null &&
      body.trigger_sensitivity == null
    ) {
      showError("Change at least one occupancy knob before applying.");
      return;
    }
    for (const field of occupancyKnobFields) {
      const rangeError = field.validateChanged();
      if (rangeError != null) {
        showError(rangeError);
        return;
      }
    }
    applyOccupancyBtn.disabled = true;
    try {
      const snap = await api.putEp1OccupancyTuning(deviceId, body);
      if (selectedValue(deviceTarget.select) !== deviceId) {
        return;
      }
      applyOccupancy(snap);
      const message = formatOccupancyAppliedMessage(snap);
      if (!snap.knobs_confirmed) {
        showError(message);
      } else {
        showSuccess(message);
      }
    } catch (err) {
      if (selectedValue(deviceTarget.select) !== deviceId) {
        return;
      }
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      if (selectedValue(deviceTarget.select) === deviceId) {
        syncDeviceControls();
      }
    }
  });

  testBtn.addEventListener("click", async () => {
    const deviceId = selectedValue(deviceTarget.select);
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
        selectedValue(deviceTarget.select) !== deviceId
      ) {
        return;
      }
      if (result.ok) {
        showSuccess(result.detail);
      } else {
        showError(result.detail);
      }
    } catch (err) {
      if (
        generation !== pskTestGeneration ||
        selectedValue(deviceTarget.select) !== deviceId
      ) {
        return;
      }
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      if (generation === pskTestGeneration) {
        syncDeviceControls();
      }
    }
  });

  clearBtn.addEventListener("click", async () => {
    clearBtn.disabled = true;
    try {
      await api.clearEp1NoisePsk();
      applyFromSettings(await api.fetchEp1NoisePskSettings());
      showSuccess("Stored EP1 Noise pre-shared key cleared.");
      await options.onDevicesChanged?.();
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      clearBtn.disabled = false;
    }
  });
}
