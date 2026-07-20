/**
 * Dashboard header strip for EP1 room sensors (occupancy + climate/light).
 *
 * Placement: same row as the global bulk-off control (#524). Read-only.
 */

import type { UIDeviceOut, UIOccupancyReadingsOut, UIStateOut } from "./types.js";

/** One EP1 sensor summarized in the header strip. */
export interface Ep1HeaderStatusSnapshot {
  humidity_pct: number | null;
  illuminance_lx: number | null;
  label: string;
  occupancy: "clear" | "occupied" | "unknown";
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
    occupancy: "occupied",
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

export function formatEp1HeaderOccupancy(
  occupancy: Ep1HeaderStatusSnapshot["occupancy"],
): { compact: string; full: string } {
  switch (occupancy) {
    case "occupied":
      return { compact: "Occ", full: "Occupied" };
    case "clear":
      return { compact: "Clr", full: "Clear" };
    case "unknown":
      return { compact: "?", full: "Unknown" };
    default: {
      const _exhaustive: never = occupancy;
      throw new Error(`Expected occupancy state, got ${_exhaustive as string}`);
    }
  }
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
  return {
    compact: cLabel,
    full: `${cLabel} / ${fahrenheit.toFixed(1)} °F`,
  };
}

function createEp1HeaderStatusDevice(
  snapshot: Ep1HeaderStatusSnapshot,
): HTMLElement {
  const row = document.createElement("div");
  row.className = "ep1-header-status-device";
  row.dataset["occupancy"] = snapshot.occupancy;

  const label = document.createElement("span");
  label.className = "ep1-header-status-label";
  label.textContent = snapshot.label;
  row.append(label);

  const occupancy = formatEp1HeaderOccupancy(snapshot.occupancy);
  row.append(
    createMetricSpan("occupancy", occupancy.full, occupancy.compact),
  );

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
  const occupancy =
    device.state === "occupied" || device.state === "clear"
      ? device.state
      : "unknown";
  return {
    humidity_pct: readings?.humidity_pct ?? null,
    illuminance_lx: readings?.illuminance_lx ?? null,
    label: device.label,
    occupancy,
    temperature_c: readings?.temperature_c ?? null,
    temperature_f: readings?.temperature_f ?? null,
  };
}
