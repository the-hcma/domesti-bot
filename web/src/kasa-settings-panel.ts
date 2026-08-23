import { KasaCredentialsSource, ToastVariant, type KasaPirRange } from "./closed-sets.js";
// Kasa/Tapo KLAP account credentials + motion (PIR) tuning for the Settings hub.

import { api, HttpError } from "./api.js";
import { createFieldLabel, createInfoBadge } from "./rules-ui-helpers.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import {
  clearSettingsDialogStatus,
  setSettingsDialogStatus,
} from "./settings-status.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  KasaCredentialsSettingsOut,
  KasaDeviceSettingsOut,
  KasaMotionTuningOut,
  KasaMotionTuningSetIn,
} from "./types.js";

export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_INFO_DETAIL =
  "Numeric darkness cutoff for ambient gating (for example “motion when dark” in the Kasa app). Compare the live Ambient light reading under Sensors: the room counts as dark enough when the reading is at or below this limit. Lower values need a darker room before motion-when-dark can arm; higher values treat brighter rooms as dark enough. Minimum 0. Smart Control schedules still live in the Kasa app.";
export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_INFO_EXAMPLE =
  "Use a lower value for a stricter gate and a higher value to arm motion-when-dark in brighter conditions.";
export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_LABEL =
  "Ambient brightness limit";
export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_INFO_DETAIL =
  "Named shortcuts from the switch firmware (cloudy, dawn, …). Each option sets the ambient brightness limit to the number in parentheses — same scale as the custom field. Lower preset numbers need a darker room; higher numbers arm motion-when-dark in brighter conditions.";
export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_INFO_EXAMPLE =
  "Choose a device preset from the list; each switch supplies its own numeric value in parentheses.";
export const KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_LABEL =
  "Ambient limit preset";
export const KASA_MOTION_SETTINGS_AMBIENT_ENABLED_INFO_DETAIL =
  "When on, the switch’s ambient light sensor can gate motion-based lighting (for example “motion when dark” in the Kasa app). Domesti-bot only toggles the sensor enable — Smart Control rules stay in the Kasa app.";
export const KASA_MOTION_SETTINGS_AMBIENT_ENABLED_INFO_EXAMPLE =
  "Leave enabled if you want day/night gating; disable to ignore ambient light.";
export const KASA_MOTION_SETTINGS_AMBIENT_ENABLED_LABEL = "Ambient light enabled";
export const KASA_MOTION_SETTINGS_APPLY_LABEL = "Apply motion tuning";
export const KASA_MOTION_SETTINGS_APPLY_UNCONFIRMED =
  "Motion settings were sent, but the switch did not confirm all values. Refresh and retry.";
export const KASA_MOTION_SETTINGS_LEGEND = "Motion (PIR) tuning";
export const KASA_MOTION_SETTINGS_LINGER_INFO_DETAIL =
  "Seconds the relay stays on after the last detected motion (device inactivity timeout / cold time). Higher values keep the light on longer; lower values turn off sooner. Minimum 0. The Kasa app’s default Smart Control rule can overwrite this back to about 60 seconds — delete or edit that rule if changes do not stick.";
export const KASA_MOTION_SETTINGS_LINGER_INFO_EXAMPLE =
  "Set 120 for a two-minute linger after you walk past; try 30 when you want the light to drop quickly.";
export const KASA_MOTION_SETTINGS_LINGER_LABEL = "Linger after motion (s)";
export const KASA_MOTION_SETTINGS_NO_DEVICES =
  "No Kasa switches with PIR/motion were discovered. KS200M-class wall switches appear here after discovery.";
export const KASA_MOTION_SETTINGS_PIR_ENABLED_INFO_DETAIL =
  "Turns the wall-switch motion (PIR) detector on or off. When off, the switch will not report motion for Smart Control or automations that depend on it.";
export const KASA_MOTION_SETTINGS_PIR_ENABLED_INFO_EXAMPLE =
  "Disable PIR temporarily while painting near the switch so lights do not keep firing.";
export const KASA_MOTION_SETTINGS_PIR_ENABLED_LABEL = "PIR enabled";
export const KASA_MOTION_SETTINGS_PIR_RANGE_INFO_DETAIL =
  "How far out the PIR watches: Near, Mid, Far, or Custom. On KS200M-class switches, TP-Link documents Near at about 5 ft, Mid at about 15 ft, and Far at about 25 ft. Far covers the largest area; Near ignores distant movement. This is separate from PIR threshold sensitivity. Choosing a preset updates the device range; setting a threshold in the same Apply writes Custom instead.";
export const KASA_MOTION_SETTINGS_PIR_RANGE_INFO_EXAMPLE =
  "Use Near in a hallway so someone walking past the doorway does not trip the light.";
export const KASA_MOTION_SETTINGS_PIR_RANGE_LABEL = "PIR range";
export const KASA_MOTION_SETTINGS_PIR_THRESHOLD_INFO_DETAIL =
  "0–100 fine-tunes how easily motion counts as a trigger (writes Custom range on the device; Near/Mid/Far presets are skipped when you change threshold in the same Apply). Higher values are more sensitive (trip on smaller movements); lower values need a stronger signal. 100 = most sensitive; 0 = least.";
export const KASA_MOTION_SETTINGS_PIR_THRESHOLD_INFO_EXAMPLE =
  "If pets or hallway traffic cause false triggers, lower the threshold; raise it toward 100 only when you need the switch to notice subtle movement.";
export const KASA_MOTION_SETTINGS_PIR_THRESHOLD_LABEL = "PIR threshold (0–100)";
export const KASA_MOTION_SETTINGS_REFRESH_LABEL = "Refresh sensors";
export const KASA_MOTION_SETTINGS_SENSORS_HEADING = "Sensors";
export const KASA_MOTION_SETTINGS_SENSORS_INFO_DETAIL =
  "Read-only polled snapshots from the switch: live PIR / ambient readings plus low-level Motion ADC and signed PIR value. Ambient light: higher readings mean a brighter room (compare against your ambient brightness limit). PIR percent is a signed, ADC-derived value relative to the sensor midpoint; use abs(PIR percent) or PIR triggered for activity strength. Use Refresh sensors to update; short motion can be missed between polls.";
export const KASA_MOTION_SETTINGS_SENSORS_INFO_EXAMPLE =
  "Wave at the switch, then Refresh sensors — PIR triggered may flip to yes; if percent looks stuck, compare ADC value against ADC min / mid / max.";
export const KASA_MOTION_SETTINGS_TARGET_DEVICE_LABEL = "Target device";

const KASA_MOTION_AMBIENT_LIMIT_CUSTOM = "__custom__";

/**
 * Build ``ambient_brightness_limit`` for Apply when the field differs from baseline.
 */
export function kasaAmbientBrightnessLimitIfChanged(
  limitRaw: string,
  baselineLimit: number | null,
): { error: string } | { value: number | null } {
  const trimmed = limitRaw.trim();
  if (trimmed === "") {
    return { value: null };
  }
  const next = Number(trimmed);
  if (!Number.isFinite(next) || !Number.isInteger(next)) {
    return { error: "Ambient brightness limit must be a whole number." };
  }
  if (next < 0) {
    return { error: "Ambient brightness limit must be 0 or greater." };
  }
  if (baselineLimit != null && next === baselineLimit) {
    return { value: null };
  }
  return { value: next };
}

/**
 * Build ``inactivity_timeout_ms`` for Apply when the linger field differs from
 * the displayed baseline. Comparing display seconds (not raw ms) avoids rewriting
 * a non-whole-second device value (e.g. 1500 → shown as 2) on unrelated edits.
 */
export function kasaInactivityTimeoutMsIfLingerChanged(
  lingerSecondsRaw: string,
  baselineInactivityTimeoutMs: number,
): { error: string } | { ms: number | null } {
  const trimmed = lingerSecondsRaw.trim();
  if (trimmed === "") {
    return { ms: null };
  }
  const nextLingerSeconds = Number(trimmed);
  if (!Number.isFinite(nextLingerSeconds) || !Number.isInteger(nextLingerSeconds)) {
    return { error: "Linger after motion must be a whole number of seconds." };
  }
  if (nextLingerSeconds < 0) {
    return { error: "Linger after motion must be 0 or greater." };
  }
  if (nextLingerSeconds === kasaLingerDisplaySeconds(baselineInactivityTimeoutMs)) {
    return { ms: null };
  }
  const nextMs = nextLingerSeconds * 1000;
  if (!Number.isSafeInteger(nextMs)) {
    return { error: "Linger after motion is too large." };
  }
  return { ms: nextMs };
}

/** Whole seconds shown in the linger field for a device timeout in milliseconds. */
export function kasaLingerDisplaySeconds(inactivityTimeoutMs: number): number {
  return Math.round(inactivityTimeoutMs / 1000);
}

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
    "Edit PIR range / threshold, linger after motion, ambient brightness limit, and enable switches on motion-capable wall switches (for example KS200M). Sensor readings below are read-only — refresh to update; short motion can be missed between polls. Smart Control schedules stay in the Kasa app.";
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

/** Label text associated via ``htmlFor``; info badge stays outside the ``<label>``. */
function createMotionField(
  label: string,
  info: { detail: string; example: string },
  control: HTMLElement,
): HTMLDivElement {
  const root = document.createElement("div");
  const isCheckbox =
    control instanceof HTMLInputElement && control.type === "checkbox";
  root.className = isCheckbox
    ? "settings-dialog-field kasa-motion-check-field"
    : "settings-dialog-field";
  const controlId =
    control.id !== ""
      ? control.id
      : `kasa-motion-${control.getAttribute("name") ?? "field"}`;
  control.id = controlId;
  const labelRow = document.createElement("span");
  labelRow.className = "rules-field-label-row";
  const textLabel = document.createElement("label");
  textLabel.htmlFor = controlId;
  textLabel.textContent = label;
  labelRow.append(textLabel, createInfoBadge(label, info.detail, info.example));
  if (isCheckbox) {
    // Checkbox + label + info on one row (badge remains outside the label).
    root.append(control, labelRow);
  } else {
    root.append(labelRow, control);
  }
  return root;
}

function createSensorItem(label: string): {
  item: HTMLDivElement;
  value: HTMLSpanElement;
} {
  const item = document.createElement("div");
  item.className = "kasa-motion-sensor";
  const labelEl = document.createElement("span");
  labelEl.className = "kasa-motion-sensor-label";
  labelEl.textContent = label;
  const value = document.createElement("span");
  value.className = "kasa-motion-sensor-value";
  value.textContent = "—";
  item.append(labelEl, value);
  return { item, value };
}

function formatOptionalNumber(value: number | null): string {
  return value == null ? "—" : String(value);
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
  credsSection.className = "settings-dialog-fieldset kasa-credentials-section";
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

  const pirEnabledInput = document.createElement("input");
  pirEnabledInput.type = "checkbox";
  pirEnabledInput.name = "pir_enabled";
  const pirEnabledField = createMotionField(
    KASA_MOTION_SETTINGS_PIR_ENABLED_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_PIR_ENABLED_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_PIR_ENABLED_INFO_EXAMPLE,
    },
    pirEnabledInput,
  );

  const pirRangeSelect = document.createElement("select");
  pirRangeSelect.name = "pir_range";
  const pirRangeField = createMotionField(
    KASA_MOTION_SETTINGS_PIR_RANGE_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_PIR_RANGE_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_PIR_RANGE_INFO_EXAMPLE,
    },
    pirRangeSelect,
  );

  const pirThresholdInput = document.createElement("input");
  pirThresholdInput.type = "number";
  pirThresholdInput.name = "pir_threshold";
  pirThresholdInput.min = "0";
  pirThresholdInput.max = "100";
  pirThresholdInput.step = "1";
  const pirThresholdField = createMotionField(
    KASA_MOTION_SETTINGS_PIR_THRESHOLD_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_PIR_THRESHOLD_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_PIR_THRESHOLD_INFO_EXAMPLE,
    },
    pirThresholdInput,
  );

  const lingerInput = document.createElement("input");
  lingerInput.type = "number";
  lingerInput.name = "linger_seconds";
  lingerInput.min = "0";
  lingerInput.step = "1";
  const lingerField = createMotionField(
    KASA_MOTION_SETTINGS_LINGER_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_LINGER_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_LINGER_INFO_EXAMPLE,
    },
    lingerInput,
  );

  const knobsRow = document.createElement("div");
  knobsRow.className = "settings-dialog-field-row kasa-motion-knobs-row";
  knobsRow.append(pirRangeField, pirThresholdField, lingerField);

  const ambientEnabledInput = document.createElement("input");
  ambientEnabledInput.type = "checkbox";
  ambientEnabledInput.name = "ambient_light_enabled";
  const ambientEnabledField = createMotionField(
    KASA_MOTION_SETTINGS_AMBIENT_ENABLED_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_AMBIENT_ENABLED_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_AMBIENT_ENABLED_INFO_EXAMPLE,
    },
    ambientEnabledInput,
  );

  const checksRow = document.createElement("div");
  checksRow.className = "settings-dialog-field-row kasa-motion-checks-row";
  checksRow.append(pirEnabledField, ambientEnabledField);

  const ambientLimitPresetSelect = document.createElement("select");
  ambientLimitPresetSelect.name = "ambient_brightness_limit_preset";
  const ambientLimitPresetField = createMotionField(
    KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_PRESET_INFO_EXAMPLE,
    },
    ambientLimitPresetSelect,
  );

  const ambientLimitInput = document.createElement("input");
  ambientLimitInput.type = "number";
  ambientLimitInput.name = "ambient_brightness_limit";
  ambientLimitInput.min = "0";
  ambientLimitInput.step = "1";
  const ambientLimitField = createMotionField(
    KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_LABEL,
    {
      detail: KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_AMBIENT_BRIGHTNESS_LIMIT_INFO_EXAMPLE,
    },
    ambientLimitInput,
  );

  const ambientLimitRow = document.createElement("div");
  ambientLimitRow.className =
    "settings-dialog-field-row kasa-motion-knobs-row kasa-motion-ambient-limit-row";
  ambientLimitRow.append(ambientLimitPresetField, ambientLimitField);

  const sensorsHeading = document.createElement("h3");
  sensorsHeading.className =
    "settings-dialog-subheading kasa-motion-sensors-heading";
  sensorsHeading.append(
    createFieldLabel(KASA_MOTION_SETTINGS_SENSORS_HEADING, {
      detail: KASA_MOTION_SETTINGS_SENSORS_INFO_DETAIL,
      example: KASA_MOTION_SETTINGS_SENSORS_INFO_EXAMPLE,
    }),
  );

  const sensors = document.createElement("div");
  sensors.className = "settings-dialog-lead kasa-motion-sensors";
  const triggered = createSensorItem("PIR triggered");
  const percent = createSensorItem("PIR percent");
  const ambientLight = createSensorItem("Ambient light");
  sensors.append(triggered.item, percent.item, ambientLight.item);

  const debugSensors = document.createElement("div");
  debugSensors.className =
    "settings-dialog-lead kasa-motion-sensors kasa-motion-debug-sensors";
  const pirValue = createSensorItem("PIR value");
  const adcValue = createSensorItem("ADC value");
  const adcMin = createSensorItem("ADC min");
  const adcMid = createSensorItem("ADC mid");
  const adcMax = createSensorItem("ADC max");
  debugSensors.append(
    pirValue.item,
    adcValue.item,
    adcMin.item,
    adcMid.item,
    adcMax.item,
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

  motionSection.append(
    motionStatus,
    knobsRow,
    checksRow,
    ambientLimitRow,
    sensorsHeading,
    sensors,
    debugSensors,
    motionActions,
  );
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

  const showStatusMessage = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(status, message, tone);
  };

  const hideStatus = (): void => {
    clearSettingsDialogStatus(status);
  };

  const showMotionStatus = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(motionStatus, message, tone);
  };

  const hideMotionStatus = (): void => {
    clearSettingsDialogStatus(motionStatus);
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
    ambientEnabledField.hidden = !ambientReady && baseline != null;
    ambientEnabledInput.disabled = !ambientReady || deviceLoading;
    ambientLimitRow.hidden = !ambientReady && baseline != null;
    ambientLimitPresetSelect.disabled = !ambientReady || deviceLoading;
    ambientLimitInput.disabled = !ambientReady || deviceLoading;
  };

  const syncAmbientLimitPresetSelection = (): void => {
    const raw = ambientLimitInput.value.trim();
    const next = Number(raw);
    let matched = false;
    if (raw !== "" && Number.isFinite(next) && Number.isInteger(next)) {
      for (const option of Array.from(ambientLimitPresetSelect.options)) {
        if (
          option.value !== KASA_MOTION_AMBIENT_LIMIT_CUSTOM &&
          Number(option.value) === next
        ) {
          ambientLimitPresetSelect.value = option.value;
          matched = true;
          break;
        }
      }
    }
    if (!matched) {
      ambientLimitPresetSelect.value = KASA_MOTION_AMBIENT_LIMIT_CUSTOM;
    }
  };

  const applyMotionSnapshot = (snap: KasaMotionTuningOut | null): void => {
    baseline = snap;
    if (snap == null) {
      pirEnabledInput.checked = false;
      pirRangeSelect.replaceChildren();
      pirThresholdInput.value = "";
      lingerInput.value = "";
      ambientEnabledInput.checked = false;
      ambientLimitPresetSelect.replaceChildren();
      ambientLimitInput.value = "";
      triggered.value.textContent = "—";
      percent.value.textContent = "—";
      ambientLight.value.textContent = "—";
      pirValue.value.textContent = "—";
      adcValue.value.textContent = "—";
      adcMin.value.textContent = "—";
      adcMid.value.textContent = "—";
      adcMax.value.textContent = "—";
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
    lingerInput.value = String(kasaLingerDisplaySeconds(snap.inactivity_timeout_ms));
    ambientEnabledInput.checked = snap.ambient_light_enabled === true;
    ambientLimitPresetSelect.replaceChildren();
    for (const preset of snap.ambient_brightness_limit_presets) {
      const option = document.createElement("option");
      option.value = String(preset.value);
      option.textContent = `${preset.name} (${String(preset.value)})`;
      ambientLimitPresetSelect.append(option);
    }
    const customOption = document.createElement("option");
    customOption.value = KASA_MOTION_AMBIENT_LIMIT_CUSTOM;
    customOption.textContent = "Custom";
    ambientLimitPresetSelect.append(customOption);
    ambientLimitInput.value =
      snap.ambient_brightness_limit == null
        ? ""
        : String(snap.ambient_brightness_limit);
    syncAmbientLimitPresetSelection();
    triggered.value.textContent = snap.pir_triggered ? "yes" : "no";
    percent.value.textContent =
      snap.pir_percent == null ? "—" : snap.pir_percent.toFixed(2);
    ambientLight.value.textContent =
      snap.ambient_light == null ? "—" : `${String(snap.ambient_light)}%`;
    pirValue.value.textContent = formatOptionalNumber(snap.pir_value);
    adcValue.value.textContent = formatOptionalNumber(snap.adc_value);
    adcMin.value.textContent = formatOptionalNumber(snap.adc_min);
    adcMid.value.textContent = formatOptionalNumber(snap.adc_mid);
    adcMax.value.textContent = formatOptionalNumber(snap.adc_max);
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
        ToastVariant.Error,
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
        ToastVariant.Error,
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
        ToastVariant.Error,
      );
    }
  };

  const saveCredentials = (): void => {
    void (async () => {
      const username = emailInput.value.trim();
      const password = passwordInput.value;
      if (!username) {
        showStatusMessage(
          "Enter the Kasa/Tapo account email before saving.",
          ToastVariant.Error,
        );
        return;
      }
      if (!password) {
        showStatusMessage(
          "Enter the account password before saving.",
          ToastVariant.Error,
        );
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
            ToastVariant.Success,
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
          ToastVariant.Error,
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
        showStatusMessage(
          result.detail,
          result.ok ? ToastVariant.Success : ToastVariant.Error,
        );
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Test failed.",
          ToastVariant.Error,
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
          ToastVariant.Error,
        );
      }
    })();
  });

  deviceTarget.select.addEventListener("change", () => {
    void loadMotionPanel();
  });

  ambientLimitPresetSelect.addEventListener("change", () => {
    if (ambientLimitPresetSelect.value === KASA_MOTION_AMBIENT_LIMIT_CUSTOM) {
      return;
    }
    ambientLimitInput.value = ambientLimitPresetSelect.value;
  });

  ambientLimitInput.addEventListener("input", () => {
    syncAmbientLimitPresetSelection();
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
          showMotionStatus(
            "PIR threshold must be between 0 and 100.",
            ToastVariant.Error,
          );
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
      const lingerResult = kasaInactivityTimeoutMsIfLingerChanged(
        lingerInput.value,
        baseline.inactivity_timeout_ms,
      );
      if ("error" in lingerResult) {
        showMotionStatus(lingerResult.error, ToastVariant.Error);
        return;
      }
      if (lingerResult.ms != null) {
        body.inactivity_timeout_ms = lingerResult.ms;
      }
      if (baseline.ambient_available) {
        const nextAmbient = ambientEnabledInput.checked;
        if (nextAmbient !== (baseline.ambient_light_enabled === true)) {
          body.ambient_light_enabled = nextAmbient;
        }
        const limitResult = kasaAmbientBrightnessLimitIfChanged(
          ambientLimitInput.value,
          baseline.ambient_brightness_limit,
        );
        if ("error" in limitResult) {
          showMotionStatus(limitResult.error, ToastVariant.Error);
          return;
        }
        if (limitResult.value != null) {
          body.ambient_brightness_limit = limitResult.value;
        }
      }
      if (
        body.pir_enabled == null &&
        body.pir_range == null &&
        body.pir_threshold == null &&
        body.inactivity_timeout_ms == null &&
        body.ambient_light_enabled == null &&
        body.ambient_brightness_limit == null
      ) {
        showMotionStatus("No motion settings changed.", ToastVariant.Info);
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
          showMotionStatus(
            KASA_MOTION_SETTINGS_APPLY_UNCONFIRMED,
            ToastVariant.Error,
          );
        }
      } catch (err) {
        showMotionStatus(
          err instanceof HttpError
            ? err.detail || err.message
            : "Apply motion tuning failed.",
          ToastVariant.Error,
        );
      } finally {
        syncMotionControls();
      }
    })();
  });

  await refreshStatus();
  await refreshMotionDevices();
}
