/**
 * Dashboard header strip for EP1 climate/light readings + occupancy glyph.
 *
 * Placement: climate strip beside brand; occupancy person/ghost immediately
 * right of the global bulk-off control (#574). Read-only.
 */

import { formatDeviceIdentityTooltip } from "./device-identity-tooltip.js";
import {
  DeviceConditionState,
  DeviceFamilyId,
  type UIDeviceOut,
  type UIDeviceState,
  type UIOccupancyReadingsOut,
  type UIStateOut,
} from "./types.js";

/** Align with ``EP1_HEADER_EXPECTED_REFRESH_PERIOD_S`` in ``app.ep1_header_freshness``. */
export const EP1_HEADER_EXPECTED_REFRESH_PERIOD_S = 5;

/** Stale window: three expected refresh periods (15s with the default period). */
export const EP1_HEADER_STALE_AFTER_S =
  3 * EP1_HEADER_EXPECTED_REFRESH_PERIOD_S;

export const EP1_HEADER_OCCUPANCY_ARIA_CLEAR = "Room clear";
export const EP1_HEADER_OCCUPANCY_ARIA_OCCUPIED = "Room occupied";

/** Aggregate occupancy cue for the header glyph (multi-EP1 rule). */
export const Ep1HeaderOccupancyGlyph = {
  Clear: "clear",
  Occupied: "occupied",
} as const;
export type Ep1HeaderOccupancyGlyph =
  (typeof Ep1HeaderOccupancyGlyph)[keyof typeof Ep1HeaderOccupancyGlyph];

const SVG_NS = "http://www.w3.org/2000/svg";

/** One EP1 sensor summarized in the header strip. */
export interface Ep1HeaderStatusSnapshot {
  host: string | null;
  humidity_pct: number | null;
  identity_details: readonly string[];
  illuminance_lx: number | null;
  label: string;
  mac_address: string;
  occupancy_state: UIDeviceState;
  responding: boolean;
  temperature_c: number | null;
  temperature_f: number | null;
}

/** Build header-strip snapshots from live ``/v1/ui/state`` EP1 tiles. */
export function ep1HeaderStatusFromUiState(
  state: UIStateOut,
): Ep1HeaderStatusSnapshot[] {
  const snapshots: Ep1HeaderStatusSnapshot[] = [];
  for (const family of state.families) {
    if (family.id !== DeviceFamilyId.Ep1) {
      continue;
    }
    for (const device of family.devices) {
      snapshots.push(ep1HeaderStatusFromDevice(device));
    }
  }
  return snapshots;
}

/**
 * Multi-EP1 occupancy for the header glyph:
 * occupied if any *responding* EP1 is occupied; else clear if any responding
 * EP1 is clear; else null (hide — including when all sensors are stale).
 */
export function ep1HeaderOccupancyGlyphFromUiState(
  state: UIStateOut,
): Ep1HeaderOccupancyGlyph | null {
  let sawClear = false;
  for (const family of state.families) {
    if (family.id !== DeviceFamilyId.Ep1) {
      continue;
    }
    for (const device of family.devices) {
      const readings = device.occupancy_readings;
      const responding =
        readings?.responding ??
        isEp1HeaderResponding(readings?.last_heard_at ?? null);
      if (!responding) {
        continue;
      }
      if (device.state === DeviceConditionState.Occupied) {
        return Ep1HeaderOccupancyGlyph.Occupied;
      }
      if (device.state === DeviceConditionState.Clear) {
        sawClear = true;
      }
    }
  }
  return sawClear ? Ep1HeaderOccupancyGlyph.Clear : null;
}

/** Mount the read-only EP1 status strip (or ``null`` when there is nothing to show). */
export function createEp1HeaderStatusStrip(
  snapshots: readonly Ep1HeaderStatusSnapshot[],
): HTMLElement | null {
  if (snapshots.length === 0) {
    return null;
  }
  const aside = document.createElement("aside");
  aside.className = "ep1-header-status";
  aside.setAttribute("aria-label", "Room sensors");
  for (const snapshot of snapshots) {
    aside.append(createEp1HeaderStatusDevice(snapshot));
  }
  return aside;
}

/** Person (occupied) or ghost (clear) control for the header actions row. */
export function createEp1HeaderOccupancyGlyph(
  kind: Ep1HeaderOccupancyGlyph,
  snapshots: readonly Ep1HeaderStatusSnapshot[] = [],
): HTMLElement {
  const span = document.createElement("span");
  span.className = "ep1-header-occupancy-glyph";
  span.dataset["occupancy"] = kind;
  span.setAttribute(
    "aria-label",
    kind === Ep1HeaderOccupancyGlyph.Occupied
      ? EP1_HEADER_OCCUPANCY_ARIA_OCCUPIED
      : EP1_HEADER_OCCUPANCY_ARIA_CLEAR,
  );
  span.setAttribute("role", "img");
  span.title = formatEp1HeaderOccupancyTooltip(kind, snapshots);
  span.append(
    kind === Ep1HeaderOccupancyGlyph.Occupied
      ? createPersonSvg()
      : createGhostSvg(),
  );
  return span;
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

/** Desktop hover copy: occupancy heading plus the EP1(s) backing the glyph. */
export function formatEp1HeaderOccupancyTooltip(
  kind: Ep1HeaderOccupancyGlyph,
  snapshots: readonly Ep1HeaderStatusSnapshot[],
): string {
  const heading =
    kind === Ep1HeaderOccupancyGlyph.Occupied
      ? EP1_HEADER_OCCUPANCY_ARIA_OCCUPIED
      : EP1_HEADER_OCCUPANCY_ARIA_CLEAR;
  const sources = occupancyTooltipSources(snapshots);
  if (sources.length === 0) {
    return heading;
  }
  const includeStateSuffix = sources.length > 1;
  const blocks: string[] = [heading];
  for (const snapshot of sources) {
    const label = occupancyTooltipDeviceLabel(snapshot, includeStateSuffix);
    blocks.push("");
    blocks.push(
      formatDeviceIdentityTooltip(
        {
          host: snapshot.host,
          identity_details: snapshot.identity_details,
          label,
          mac_address: snapshot.mac_address,
        },
        { includeLabel: true },
      ),
    );
  }
  return blocks.join("\n");
}

export function formatEp1HeaderTemperature(
  readings: Pick<Ep1HeaderStatusSnapshot, "temperature_c" | "temperature_f">,
): { compactC: string; compactF: string; fullC: string; fullF: string } | null {
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
  return {
    // Separate metrics so comfortable uses the same · separator as humidity/lux.
    compactC: `${celsius.toFixed(1)}°C`,
    compactF: `${fahrenheit.toFixed(1)}°F`,
    fullC: `${celsius.toFixed(1)} °C`,
    fullF: `${fahrenheit.toFixed(1)} °F`,
  };
}

/** Client-side fallback when ``responding`` is absent from older payloads. */
export function isEp1HeaderResponding(
  lastHeardAt: number | null | undefined,
  nowMs: number = Date.now(),
): boolean {
  if (lastHeardAt == null) {
    return false;
  }
  const ageS = nowMs / 1000 - lastHeardAt;
  return ageS <= EP1_HEADER_STALE_AFTER_S;
}

function createEp1HeaderStatusDevice(
  snapshot: Ep1HeaderStatusSnapshot,
): HTMLElement {
  const row = document.createElement("div");
  row.className = "ep1-header-status-device";
  row.dataset["responding"] = snapshot.responding ? "true" : "false";
  row.title = formatDeviceIdentityTooltip(snapshot, { includeLabel: true });

  const temp = formatEp1HeaderTemperature(snapshot);
  if (temp != null) {
    row.append(
      createMetricSpan("temperature", temp.fullC, temp.compactC, snapshot.responding),
    );
    row.append(
      createMetricSpan(
        "temperature-f",
        temp.fullF,
        temp.compactF,
        snapshot.responding,
      ),
    );
  }

  const humidity = formatEp1HeaderHumidity(snapshot.humidity_pct);
  if (humidity != null) {
    row.append(
      createMetricSpan("humidity", humidity, humidity, snapshot.responding),
    );
  }

  const lux = formatEp1HeaderIlluminance(snapshot.illuminance_lx);
  if (lux != null) {
    row.append(
      createMetricSpan("illuminance", lux, lux, snapshot.responding),
    );
  }

  return row;
}

function createGhostSvg(): SVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "22");
  svg.setAttribute("height", "22");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("ep1-header-occupancy-svg");
  const body = document.createElementNS(SVG_NS, "path");
  body.setAttribute(
    "d",
    "M12 2a7 7 0 0 0-7 7v11l2.5-1.5L10 20l2-1.5L14 20l2.5-1.5L19 20V9a7 7 0 0 0-7-7z",
  );
  body.setAttribute("fill", "none");
  body.setAttribute("stroke", "currentColor");
  body.setAttribute("stroke-width", "2");
  body.setAttribute("stroke-linecap", "round");
  body.setAttribute("stroke-linejoin", "round");
  body.classList.add("ep1-header-occupancy-fill");
  const eyeL = document.createElementNS(SVG_NS, "circle");
  eyeL.setAttribute("cx", "9");
  eyeL.setAttribute("cy", "10");
  eyeL.setAttribute("r", "1");
  eyeL.setAttribute("fill", "currentColor");
  const eyeR = document.createElementNS(SVG_NS, "circle");
  eyeR.setAttribute("cx", "15");
  eyeR.setAttribute("cy", "10");
  eyeR.setAttribute("r", "1");
  eyeR.setAttribute("fill", "currentColor");
  svg.append(body, eyeL, eyeR);
  return svg;
}

function createMetricSpan(
  metric: string,
  comfortable: string,
  compact: string,
  responding: boolean,
): HTMLElement {
  const span = document.createElement("span");
  span.className = "ep1-header-status-metric";
  span.dataset["metric"] = metric;
  span.dataset["responding"] = responding ? "true" : "false";
  const full = document.createElement("span");
  full.className = "ep1-header-status-full";
  full.textContent = comfortable;
  const short = document.createElement("span");
  short.className = "ep1-header-status-compact";
  short.textContent = compact;
  span.append(full, short);
  return span;
}

function createPersonSvg(): SVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "22");
  svg.setAttribute("height", "22");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("ep1-header-occupancy-svg");
  const head = document.createElementNS(SVG_NS, "circle");
  head.setAttribute("cx", "12");
  head.setAttribute("cy", "7");
  head.setAttribute("r", "3");
  head.setAttribute("fill", "none");
  head.setAttribute("stroke", "currentColor");
  head.setAttribute("stroke-width", "2");
  head.classList.add("ep1-header-occupancy-fill");
  const body = document.createElementNS(SVG_NS, "path");
  body.setAttribute("d", "M6 20c0-3.3 2.7-6 6-6s6 2.7 6 6");
  body.setAttribute("fill", "none");
  body.setAttribute("stroke", "currentColor");
  body.setAttribute("stroke-width", "2");
  body.setAttribute("stroke-linecap", "round");
  body.classList.add("ep1-header-occupancy-fill");
  svg.append(head, body);
  return svg;
}

function ep1HeaderStatusFromDevice(device: UIDeviceOut): Ep1HeaderStatusSnapshot {
  const readings: UIOccupancyReadingsOut | null | undefined =
    device.occupancy_readings;
  const responding =
    readings?.responding ??
    isEp1HeaderResponding(readings?.last_heard_at ?? null);
  return {
    host: (device.host ?? "").trim() || null,
    humidity_pct: readings?.humidity_pct ?? null,
    identity_details: device.identity_details ?? [],
    illuminance_lx: readings?.illuminance_lx ?? null,
    label: device.label,
    mac_address: device.mac_address,
    occupancy_state: device.state,
    responding,
    temperature_c: readings?.temperature_c ?? null,
    temperature_f: readings?.temperature_f ?? null,
  };
}

function occupancyTooltipDeviceLabel(
  snapshot: Ep1HeaderStatusSnapshot,
  includeStateSuffix: boolean,
): string {
  const base = snapshot.label.trim() || snapshot.mac_address;
  if (!includeStateSuffix) {
    return base;
  }
  return `${base} (${snapshot.occupancy_state})`;
}

function occupancyTooltipSources(
  snapshots: readonly Ep1HeaderStatusSnapshot[],
): Ep1HeaderStatusSnapshot[] {
  const responding = snapshots.filter((snapshot) => snapshot.responding);
  return responding.length > 0 ? responding : [...snapshots];
}
