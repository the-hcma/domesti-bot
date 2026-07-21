/**
 * Dashboard header strip for EP1 climate/light readings.
 *
 * Placement: same row as the global bulk-off control (#524). Read-only.
 * Occupancy is intentionally omitted from this strip (room sensors still
 * expose it on tiles / rule conditions elsewhere).
 */

import type { UIDeviceOut, UIOccupancyReadingsOut, UIStateOut } from "./types.js";

/** One EP1 sensor summarized in the header strip. */
export interface Ep1HeaderStatusSnapshot {
  humidity_pct: number | null;
  illuminance_lx: number | null;
  label: string;
  temperature_c: number | null;
  temperature_f: number | null;
}

/**
 * Placeholder readings so the header strip can be designed without LAN hardware.
 *
 * TODO(ep1-header-live): delete this mock and drive the strip from
 * ``ep1HeaderStatusFromUiState(state)`` (or a dedicated UI payload) once an
 * EP1 is discovered on the network.
 */
export const MOCK_EP1_HEADER_STATUS: readonly Ep1HeaderStatusSnapshot[] = [
  {
    humidity_pct: 42,
    illuminance_lx: 180,
    label: "Office EP1",
    temperature_c: 22.5,
    temperature_f: 72.5,
  },
];

/** Build header-strip snapshots from live ``/v1/ui/state`` EP1 tiles. */
export function ep1HeaderStatusFromUiState(
  state: UIStateOut,
): Ep1HeaderStatusSnapshot[] {
  const snapshots: Ep1HeaderStatusSnapshot[] = [];
  for (const family of state.families) {
    if (family.id !== "ep1") {
      continue;
    }
    for (const device of family.devices) {
      snapshots.push(ep1HeaderStatusFromDevice(device));
    }
  }
  return snapshots;
}

/** Mount the read-only EP1 status strip (or ``null`` when there is nothing to show). */
export function createEp1HeaderStatusStrip(
  snapshots: readonly Ep1HeaderStatusSnapshot[],
  options: { mock: boolean },
): HTMLElement | null {
  if (snapshots.length === 0) {
    return null;
  }
  const aside = document.createElement("aside");
  aside.className = "ep1-header-status";
  aside.setAttribute("aria-label", "Room sensors");
  if (options.mock) {
    aside.dataset["mock"] = "true";
    aside.title = "Mock EP1 readings (no sensor on the LAN yet)";
  }
  for (const snapshot of snapshots) {
    aside.append(createEp1HeaderStatusDevice(snapshot));
  }
  return aside;
}

export function formatEp1HeaderHumidity(pct: number | null): string | null {
  if (pct == null) {
    return null;
  }
  return Number.isInteger(pct) ? `${String(pct)}%` : `${pct.toFixed(1)}%`;
}

export function formatEp1HeaderIlluminance(lx: number | null): string | null {
  if (lx == null) {
    return null;
  }
  return Number.isInteger(lx) ? `${String(lx)} lx` : `${lx.toFixed(1)} lx`;
}

export function formatEp1HeaderTemperature(
  readings: Pick<Ep1HeaderStatusSnapshot, "temperature_c" | "temperature_f">,
): { compact: string; full: string } | null {
  let celsius = readings.temperature_c;
  let fahrenheit = readings.temperature_f;
  if (celsius == null && fahrenheit != null) {
    celsius = ((fahrenheit - 32) * 5) / 9;
  }
  if (fahrenheit == null && celsius != null) {
    fahrenheit = (celsius * 9) / 5 + 32;
  }
  if (celsius == null || fahrenheit == null) {
    return null;
  }
  const cLabel = `${celsius.toFixed(1)} °C`;
  const fLabel = `${fahrenheit.toFixed(1)} °F`;
  const dual = `${cLabel} / ${fLabel}`;
  return {
    // Phone strip stays one row: still dual-unit, slightly tighter spacing.
    compact: `${celsius.toFixed(1)}°C/${fahrenheit.toFixed(1)}°F`,
    full: dual,
  };
}

function createEp1HeaderStatusDevice(
  snapshot: Ep1HeaderStatusSnapshot,
): HTMLElement {
  const row = document.createElement("div");
  row.className = "ep1-header-status-device";

  const label = document.createElement("span");
  label.className = "ep1-header-status-label";
  label.textContent = snapshot.label;
  row.append(label);

  const temp = formatEp1HeaderTemperature(snapshot);
  if (temp != null) {
    row.append(createMetricSpan("temperature", temp.full, temp.compact));
  }

  const humidity = formatEp1HeaderHumidity(snapshot.humidity_pct);
  if (humidity != null) {
    row.append(createMetricSpan("humidity", humidity, humidity));
  }

  const lux = formatEp1HeaderIlluminance(snapshot.illuminance_lx);
  if (lux != null) {
    row.append(createMetricSpan("illuminance", lux, lux));
  }

  return row;
}

function createMetricSpan(
  metric: string,
  comfortable: string,
  compact: string,
): HTMLElement {
  const span = document.createElement("span");
  span.className = "ep1-header-status-metric";
  span.dataset["metric"] = metric;
  const full = document.createElement("span");
  full.className = "ep1-header-status-full";
  full.textContent = comfortable;
  const short = document.createElement("span");
  short.className = "ep1-header-status-compact";
  short.textContent = compact;
  span.append(full, short);
  return span;
}

function ep1HeaderStatusFromDevice(device: UIDeviceOut): Ep1HeaderStatusSnapshot {
  const readings: UIOccupancyReadingsOut | null | undefined =
    device.occupancy_readings;
  return {
    humidity_pct: readings?.humidity_pct ?? null,
    illuminance_lx: readings?.illuminance_lx ?? null,
    label: device.label,
    temperature_c: readings?.temperature_c ?? null,
    temperature_f: readings?.temperature_f ?? null,
  };
}
