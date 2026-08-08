// Automations → Data: per-sensor collection controls + time-window charts.

import { HttpError } from "./api.js";
import {
  SensorChartWindow,
  SensorCollectionFrequencyChoice,
  SensorCollectionIntervalS,
  SensorCollectionKey,
} from "./closed-sets.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel } from "./rules-ui-helpers.js";
import type {
  SensorCollectionRetentionOut,
  SensorCollectionSampleOut,
  SensorCollectionSensorOut,
} from "./types.js";
import { showSuccessToast } from "./ui-toast.js";

const CHART_HEIGHT = 168;
const CHART_PAD_BOTTOM = 28;
const CHART_PAD_LEFT = 48;
const CHART_PAD_LEFT_OCCUPANCY = 72;
const CHART_PAD_RIGHT = 12;
const CHART_PAD_TOP = 12;
const CHART_WIDTH = 720;
const DEFAULT_RETENTION_DAYS = 60;
const INTERVAL_OPTIONS: readonly {
  label: string;
  value: SensorCollectionIntervalS;
}[] = [
  { label: "5s", value: SensorCollectionIntervalS.Five },
  { label: "15s", value: SensorCollectionIntervalS.Fifteen },
  { label: "30s", value: SensorCollectionIntervalS.Thirty },
  { label: "1m", value: SensorCollectionIntervalS.OneMinute },
  { label: "5m", value: SensorCollectionIntervalS.FiveMinutes },
];

const WINDOW_OPTIONS: readonly {
  label: string;
  value: SensorChartWindow;
}[] = [
  { label: "Last minute", value: SensorChartWindow.LastMinute },
  { label: "Last 5 minutes", value: SensorChartWindow.Last5Minutes },
  { label: "Last hour", value: SensorChartWindow.LastHour },
  { label: "Last day", value: SensorChartWindow.LastDay },
  { label: "Last week", value: SensorChartWindow.LastWeek },
];

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

function sensorKeyLabel(key: SensorCollectionKey): string {
  switch (key) {
    case SensorCollectionKey.HumidityPct:
      return "humidity";
    case SensorCollectionKey.IlluminanceLx:
      return "illuminance";
    case SensorCollectionKey.Occupancy:
      return "occupancy";
    case SensorCollectionKey.TemperatureC:
      return "temperature";
    default: {
      const _exhaustive: never = key;
      return _exhaustive;
    }
  }
}

function formatAxisReading(value: number): string {
  if (Number.isInteger(value)) {
    return String(value);
  }
  const abs = Math.abs(value);
  if (abs >= 100) {
    return value.toFixed(0);
  }
  if (abs >= 10) {
    return value.toFixed(1);
  }
  return value.toFixed(2);
}

function formatOccupancyReading(value: number): string {
  return value >= 0.5 ? "occupied" : "empty";
}

function formatReadingValue(
  value: number,
  sensorKey: SensorCollectionKey,
): string {
  if (sensorKey === SensorCollectionKey.Occupancy) {
    return formatOccupancyReading(value);
  }
  return formatAxisReading(value);
}

function formatAxisTime(epochS: number, window: SensorChartWindow): string {
  const when = new Date(epochS * 1000);
  if (
    window === SensorChartWindow.LastDay ||
    window === SensorChartWindow.LastWeek
  ) {
    // Compact so three ticks fit on phone-width cards.
    return when.toLocaleString(undefined, {
      day: "numeric",
      hour: "numeric",
      minute: "2-digit",
      month: "numeric",
    });
  }
  if (window === SensorChartWindow.LastHour) {
    return when.toLocaleTimeString(undefined, {
      hour: "numeric",
      minute: "2-digit",
    });
  }
  return when.toLocaleTimeString(undefined, {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatLastSample(
  at: number | null,
  value: number | null,
  unit: string | null,
  sensorKey: SensorCollectionKey,
): string {
  if (at === null || value === null) {
    return "No samples yet";
  }
  const when = new Date(at * 1000).toLocaleString();
  if (sensorKey === SensorCollectionKey.Occupancy) {
    return `Last: ${formatOccupancyReading(value)} · ${when}`;
  }
  const unitSuffix = unit === null || unit === "" ? "" : ` ${unit}`;
  return `Last: ${value}${unitSuffix} · ${when}`;
}

function formatSampleTooltip(
  point: SensorCollectionSampleOut,
  sensorKey: SensorCollectionKey,
): string {
  const when = new Date(point.recorded_at * 1000).toLocaleString();
  if (sensorKey === SensorCollectionKey.Occupancy) {
    return `${formatOccupancyReading(point.value)} · ${when}`;
  }
  const unitSuffix =
    point.unit === null || point.unit === "" ? "" : ` ${point.unit}`;
  return `${formatAxisReading(point.value)}${unitSuffix} · ${when}`;
}

function renderSparkline(
  svg: SVGSVGElement,
  tooltip: HTMLElement,
  points: SensorCollectionSampleOut[],
  window: SensorChartWindow,
  asOf: number,
  sensorKey: SensorCollectionKey,
): void {
  while (svg.firstChild !== null) {
    svg.removeChild(svg.firstChild);
  }
  tooltip.hidden = true;
  tooltip.textContent = "";

  const wrap = svg.parentElement;
  const chartWidth =
    wrap !== null && wrap.clientWidth > 0
      ? Math.round(wrap.clientWidth)
      : CHART_WIDTH;
  const chartHeight =
    wrap !== null && wrap.clientHeight > 0
      ? Math.round(wrap.clientHeight)
      : CHART_HEIGHT;

  svg.setAttribute("viewBox", `0 0 ${chartWidth} ${chartHeight}`);
  svg.setAttribute("role", "img");
  svg.setAttribute(
    "aria-label",
    points.length === 0
      ? "Sensor readings chart (no points in this window)"
      : `Sensor readings chart with ${points.length} samples`,
  );
  svg.setAttribute("preserveAspectRatio", "none");

  const isOccupancy = sensorKey === SensorCollectionKey.Occupancy;
  const plotLeft = isOccupancy ? CHART_PAD_LEFT_OCCUPANCY : CHART_PAD_LEFT;
  const plotRight = chartWidth - CHART_PAD_RIGHT;
  const plotTop = CHART_PAD_TOP;
  const plotBottom = chartHeight - CHART_PAD_BOTTOM;
  const plotW = plotRight - plotLeft;
  const plotH = plotBottom - plotTop;

  if (points.length === 0) {
    const empty = document.createElementNS("http://www.w3.org/2000/svg", "text");
    empty.setAttribute("x", String((plotLeft + plotRight) / 2));
    empty.setAttribute("y", String((plotTop + plotBottom) / 2));
    empty.setAttribute("text-anchor", "middle");
    empty.setAttribute("dominant-baseline", "middle");
    empty.setAttribute("class", "rules-sensor-chart-empty");
    empty.textContent = "No points in this window";
    svg.append(empty);
    return;
  }

  const durationS = windowDurationS(window);
  const end = asOf;
  const start = end - durationS;
  let minV: number;
  let maxV: number;
  let yTicks: number[];
  if (isOccupancy) {
    minV = 0;
    maxV = 1;
    yTicks = [0, 1];
  } else {
    const values = points.map((p) => p.value);
    minV = Math.min(...values);
    maxV = Math.max(...values);
    if (minV === maxV) {
      minV -= 1;
      maxV += 1;
    }
    yTicks = [minV, (minV + maxV) / 2, maxV];
  }

  const xForTime = (epochS: number): number =>
    plotLeft +
    ((Math.max(start, Math.min(end, epochS)) - start) / durationS) * plotW;
  const yForValue = (value: number): number =>
    plotBottom - ((value - minV) / (maxV - minV)) * plotH;

  const axis = document.createElementNS("http://www.w3.org/2000/svg", "g");
  axis.setAttribute("class", "rules-sensor-chart-axis");
  const xAxis = document.createElementNS("http://www.w3.org/2000/svg", "line");
  xAxis.setAttribute("x1", String(plotLeft));
  xAxis.setAttribute("y1", String(plotBottom));
  xAxis.setAttribute("x2", String(plotRight));
  xAxis.setAttribute("y2", String(plotBottom));
  const yAxis = document.createElementNS("http://www.w3.org/2000/svg", "line");
  yAxis.setAttribute("x1", String(plotLeft));
  yAxis.setAttribute("y1", String(plotTop));
  yAxis.setAttribute("x2", String(plotLeft));
  yAxis.setAttribute("y2", String(plotBottom));
  axis.append(xAxis, yAxis);

  for (const tick of yTicks) {
    const y = yForValue(tick);
    const grid = document.createElementNS("http://www.w3.org/2000/svg", "line");
    grid.setAttribute("class", "rules-sensor-chart-grid");
    grid.setAttribute("x1", String(plotLeft));
    grid.setAttribute("y1", String(y));
    grid.setAttribute("x2", String(plotRight));
    grid.setAttribute("y2", String(y));
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "rules-sensor-chart-tick");
    label.setAttribute("x", String(plotLeft - 6));
    label.setAttribute("y", String(y));
    label.setAttribute("text-anchor", "end");
    label.setAttribute("dominant-baseline", "middle");
    label.textContent = formatReadingValue(tick, sensorKey);
    axis.append(grid, label);
  }

  const useCompactXTicks =
    chartWidth < 420 ||
    window === SensorChartWindow.LastDay ||
    window === SensorChartWindow.LastWeek;
  const xTicks = useCompactXTicks
    ? [start, end]
    : [start, start + durationS / 2, end];
  for (let i = 0; i < xTicks.length; i += 1) {
    const tick = xTicks[i]!;
    const x = xForTime(tick);
    const label = document.createElementNS("http://www.w3.org/2000/svg", "text");
    label.setAttribute("class", "rules-sensor-chart-tick");
    label.setAttribute("x", String(x));
    label.setAttribute("y", String(plotBottom + 14));
    label.setAttribute(
      "text-anchor",
      i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle",
    );
    label.textContent = formatAxisTime(tick, window);
    axis.append(label);
  }
  svg.append(axis);

  const coords = points.map((point) => {
    const x = xForTime(point.recorded_at);
    const y = yForValue(point.value);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  const polyline = document.createElementNS(
    "http://www.w3.org/2000/svg",
    "polyline",
  );
  polyline.setAttribute("fill", "none");
  polyline.setAttribute("stroke", "currentColor");
  polyline.setAttribute("stroke-width", "1.75");
  polyline.setAttribute("stroke-linejoin", "round");
  polyline.setAttribute("stroke-linecap", "round");
  polyline.setAttribute("points", coords.join(" "));
  svg.append(polyline);

  const pointsLayer = document.createElementNS("http://www.w3.org/2000/svg", "g");
  pointsLayer.setAttribute("class", "rules-sensor-chart-points");
  // Cap keyboard targets so dense windows (e.g. 1-day @ 5s) stay usable.
  const enableKeyboardHits = points.length <= 96;
  const plotted = points.map((point) => ({
    point,
    cx: xForTime(point.recorded_at),
    cy: yForValue(point.value),
    tipText: formatSampleTooltip(point, sensorKey),
  }));
  const placeTooltipAt = (
    tipText: string,
    clientX: number,
    clientY: number,
  ): void => {
    tooltip.hidden = false;
    tooltip.textContent = tipText;
    const host = tooltip.offsetParent;
    if (!(host instanceof HTMLElement)) {
      return;
    }
    const wrapRect = host.getBoundingClientRect();
    const left = clientX - wrapRect.left + 10;
    const top = clientY - wrapRect.top - 28;
    tooltip.style.left = `${Math.max(4, Math.min(left, wrapRect.width - tooltip.offsetWidth - 4))}px`;
    tooltip.style.top = `${Math.max(4, Math.min(top, wrapRect.height - tooltip.offsetHeight - 4))}px`;
  };
  const nearestPlotted = (
    localX: number,
  ): (typeof plotted)[number] | null => {
    if (plotted.length === 0) {
      return null;
    }
    let best = plotted[0]!;
    let bestDist = Math.abs(best.cx - localX);
    for (let i = 1; i < plotted.length; i += 1) {
      const candidate = plotted[i]!;
      const dist = Math.abs(candidate.cx - localX);
      if (dist < bestDist) {
        best = candidate;
        bestDist = dist;
      }
    }
    return best;
  };
  const clientToSvgX = (clientX: number, clientY: number): number | null => {
    const ctm = svg.getScreenCTM();
    if (ctm === null) {
      return null;
    }
    const pt = svg.createSVGPoint();
    pt.x = clientX;
    pt.y = clientY;
    return pt.matrixTransform(ctm.inverse()).x;
  };

  for (const item of plotted) {
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("class", "rules-sensor-chart-point");
    dot.setAttribute("cx", String(item.cx));
    dot.setAttribute("cy", String(item.cy));
    dot.setAttribute("r", "2.75");
    pointsLayer.append(dot);
    if (!enableKeyboardHits) {
      continue;
    }
    const hit = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    hit.setAttribute("class", "rules-sensor-chart-point-hit");
    hit.setAttribute("cx", String(item.cx));
    hit.setAttribute("cy", String(item.cy));
    hit.setAttribute("r", "7");
    hit.setAttribute("tabindex", "0");
    hit.setAttribute("role", "img");
    hit.setAttribute("aria-label", item.tipText);
    hit.setAttribute("pointer-events", "none");
    hit.addEventListener("focus", () => {
      const rect = hit.getBoundingClientRect();
      placeTooltipAt(item.tipText, rect.left + rect.width / 2, rect.top);
    });
    hit.addEventListener("blur", () => {
      tooltip.hidden = true;
    });
    pointsLayer.append(hit);
  }

  // Single plot overlay: nearest-by-x lookup stays accurate in dense windows.
  const plotHit = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  plotHit.setAttribute("class", "rules-sensor-chart-plot-hit");
  plotHit.setAttribute("x", String(plotLeft));
  plotHit.setAttribute("y", String(plotTop));
  plotHit.setAttribute("width", String(plotW));
  plotHit.setAttribute("height", String(plotH));
  plotHit.setAttribute("fill", "transparent");
  plotHit.addEventListener("pointermove", (event) => {
    const localX = clientToSvgX(event.clientX, event.clientY);
    if (localX === null) {
      return;
    }
    const nearest = nearestPlotted(localX);
    if (nearest === null) {
      return;
    }
    placeTooltipAt(nearest.tipText, event.clientX, event.clientY);
  });
  plotHit.addEventListener("pointerleave", () => {
    tooltip.hidden = true;
  });
  pointsLayer.append(plotHit);
  svg.append(pointsLayer);
}

function windowDurationS(window: SensorChartWindow): number {
  switch (window) {
    case SensorChartWindow.LastMinute:
      return 60;
    case SensorChartWindow.Last5Minutes:
      return 300;
    case SensorChartWindow.LastHour:
      return 3600;
    case SensorChartWindow.LastDay:
      return 86_400;
    case SensorChartWindow.LastWeek:
      return 604_800;
    default: {
      const _exhaustive: never = window;
      return _exhaustive;
    }
  }
}

export async function mountSensorDataPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
): Promise<void> {
  container.replaceChildren();
  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Collect sensor readings into SQLite and inspect them on a chart. Pick a sample frequency (or don't collect), then choose a time window.";

  const retentionMount = document.createElement("div");
  retentionMount.className = "rules-sensor-retention";

  const list = document.createElement("div");
  list.className = "rules-sensor-list";

  container.append(lead, status, retentionMount, list);

  let sensors: SensorCollectionSensorOut[] = [];
  let retention: SensorCollectionRetentionOut = {
    max_age_days: DEFAULT_RETENTION_DAYS,
    unlimited: false,
  };
  try {
    const [payload, retentionPayload] = await Promise.all([
      dataSource.getSensorCollectionSensors(),
      dataSource.getSensorCollectionRetention(),
    ]);
    sensors = payload.sensors;
    retention = retentionPayload;
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not load sensor collection: ${formatError(err)}`;
    return;
  }

  mountRetentionForm(retentionMount, retention, dataSource, status);

  if (sensors.length === 0) {
    const empty = document.createElement("p");
    empty.className = "settings-dialog-status";
    empty.textContent =
      "No collectible sensors yet. EP1 occupancy / climate readings appear here after discovery.";
    list.append(empty);
    return;
  }

  for (const sensor of sensors) {
    list.append(buildSensorCard(sensor, dataSource, status));
  }
}

function mountRetentionForm(
  container: HTMLElement,
  initial: SensorCollectionRetentionOut,
  dataSource: RulesDataSource,
  status: HTMLParagraphElement,
): void {
  let current = initial;
  container.replaceChildren();

  const fieldset = document.createElement("fieldset");
  fieldset.className = "settings-dialog-fieldset";
  const legend = document.createElement("legend");
  legend.textContent = "Sample retention";
  fieldset.append(legend);

  const help = document.createElement("p");
  help.className = "settings-dialog-help";
  help.textContent =
    "Older samples are pruned automatically. Default is two months (60 days).";

  const unlimitedLabel = document.createElement("label");
  unlimitedLabel.className = "rules-sensor-enabled";
  const unlimitedCb = document.createElement("input");
  unlimitedCb.type = "checkbox";
  unlimitedCb.checked = current.unlimited;
  unlimitedLabel.append(unlimitedCb, document.createTextNode(" Keep forever (no age prune)"));

  const daysField = document.createElement("div");
  daysField.className = "settings-dialog-field";
  daysField.append(createFieldLabel("Keep for (days)"));
  const daysInput = document.createElement("input");
  daysInput.type = "number";
  daysInput.min = "1";
  daysInput.step = "1";
  daysInput.value = String(current.max_age_days);
  daysInput.disabled = current.unlimited;
  daysField.append(daysInput);

  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save retention";

  const daysRow = document.createElement("div");
  daysRow.className = "settings-dialog-field-row rules-sensor-retention-row";
  daysRow.append(daysField, saveBtn);

  const syncEnabled = (): void => {
    daysInput.disabled = unlimitedCb.checked;
  };
  unlimitedCb.addEventListener("change", syncEnabled);

  saveBtn.addEventListener("click", () => {
    void (async () => {
      status.hidden = true;
      const maxAgeDays = Number(daysInput.value);
      if (!unlimitedCb.checked && (!Number.isFinite(maxAgeDays) || maxAgeDays <= 0)) {
        status.hidden = false;
        status.textContent = "Enter a retention of at least 1 day, or enable keep forever.";
        return;
      }
      saveBtn.disabled = true;
      unlimitedCb.disabled = true;
      daysInput.disabled = true;
      try {
        current = await dataSource.saveSensorCollectionRetention({
          max_age_days:
            Number.isFinite(maxAgeDays) && maxAgeDays > 0
              ? maxAgeDays
              : DEFAULT_RETENTION_DAYS,
          unlimited: unlimitedCb.checked,
        });
        daysInput.value = String(current.max_age_days);
        unlimitedCb.checked = current.unlimited;
        showSuccessToast(
          current.unlimited
            ? "Sensor sample retention: keep forever"
            : `Sensor sample retention: ${current.max_age_days} days`,
        );
      } catch (err) {
        status.hidden = false;
        status.textContent = `Could not save retention: ${formatError(err)}`;
      } finally {
        saveBtn.disabled = false;
        unlimitedCb.disabled = false;
        syncEnabled();
      }
    })();
  });

  fieldset.append(help, unlimitedLabel, daysRow);
  container.append(fieldset);
}

function syncIntervalSelect(
  select: HTMLSelectElement,
  enabled: boolean,
  intervalS: number,
): void {
  if (!enabled) {
    select.value = SensorCollectionFrequencyChoice.DontCollect;
    return;
  }
  select.value = String(intervalS);
}

function buildSensorCard(
  initial: SensorCollectionSensorOut,
  dataSource: RulesDataSource,
  status: HTMLParagraphElement,
): HTMLElement {
  let current = initial;
  let chartWindow: SensorChartWindow = SensorChartWindow.Last5Minutes;
  /** ``null`` means live window ending at server now. */
  let chartAsOf: number | null = null;
  let lastWindowEnd = Date.now() / 1000;
  /** Last server ``as_of`` from a live (no offset) fetch; used to snap Next to live. */
  let liveWindowEnd = lastWindowEnd;

  const card = document.createElement("section");
  card.className = "rules-sensor-card";

  const title = document.createElement("h3");
  title.className = "rules-sensor-title";
  title.textContent = `${current.device_display} · ${sensorKeyLabel(current.sensor_key)}`;

  const meta = document.createElement("p");
  meta.className = "rules-sensor-meta";
  meta.textContent = formatLastSample(
    current.last_sample_at,
    current.last_value,
    current.unit,
    current.sensor_key,
  );

  const controls = document.createElement("div");
  controls.className = "rules-sensor-controls";

  const intervalField = document.createElement("div");
  intervalField.className = "settings-dialog-field rules-sensor-field-inline";
  intervalField.append(createFieldLabel("Frequency"));
  const intervalSelect = document.createElement("select");
  const dontCollectOpt = document.createElement("option");
  dontCollectOpt.value = SensorCollectionFrequencyChoice.DontCollect;
  dontCollectOpt.textContent = "Don't collect";
  intervalSelect.append(dontCollectOpt);
  for (const opt of INTERVAL_OPTIONS) {
    const option = document.createElement("option");
    option.value = String(opt.value);
    option.textContent = opt.label;
    intervalSelect.append(option);
  }
  syncIntervalSelect(intervalSelect, current.enabled, current.interval_s);
  intervalField.append(intervalSelect);

  const windowField = document.createElement("div");
  windowField.className = "settings-dialog-field rules-sensor-field-inline";
  windowField.append(createFieldLabel("Chart window"));
  const windowSelect = document.createElement("select");
  for (const opt of WINDOW_OPTIONS) {
    const option = document.createElement("option");
    option.value = opt.value;
    option.textContent = opt.label;
    if (opt.value === chartWindow) {
      option.selected = true;
    }
    windowSelect.append(option);
  }
  windowField.append(windowSelect);

  controls.append(intervalField, windowField);

  const chartNav = document.createElement("div");
  chartNav.className = "rules-sensor-chart-nav";

  const prevBtn = document.createElement("button");
  prevBtn.type = "button";
  prevBtn.className = "btn rules-sensor-chart-nav-btn";
  prevBtn.setAttribute("aria-label", "Previous period");
  prevBtn.textContent = "‹";

  const nextBtn = document.createElement("button");
  nextBtn.type = "button";
  nextBtn.className = "btn rules-sensor-chart-nav-btn";
  nextBtn.setAttribute("aria-label", "Next period");
  nextBtn.textContent = "›";
  nextBtn.disabled = true;

  const chartWrap = document.createElement("div");
  chartWrap.className = "rules-sensor-chart-wrap";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("rules-sensor-chart");
  const tooltip = document.createElement("div");
  tooltip.className = "rules-sensor-chart-tooltip";
  tooltip.hidden = true;
  tooltip.setAttribute("role", "tooltip");
  chartWrap.append(svg, tooltip);

  chartNav.append(prevBtn, chartWrap, nextBtn);

  card.append(title, meta, controls, chartNav);

  let chartRequestGeneration = 0;

  const syncNavButtons = (): void => {
    nextBtn.disabled = chartAsOf === null;
  };

  const refreshChart = async (): Promise<void> => {
    const generation = ++chartRequestGeneration;
    const requestedWindow = chartWindow;
    const requestedAsOf = chartAsOf;
    try {
      const samples = await dataSource.getSensorCollectionSamples(
        current.device_id,
        current.sensor_key,
        requestedWindow,
        requestedAsOf,
      );
      if (generation !== chartRequestGeneration) {
        return;
      }
      lastWindowEnd = samples.as_of;
      if (requestedAsOf === null) {
        liveWindowEnd = samples.as_of;
        chartAsOf = null;
      } else if (Math.abs(samples.as_of - requestedAsOf) < 0.5) {
        // Keep explicit offset when the server honored it.
        chartAsOf = samples.as_of;
      } else {
        // Server clamped toward now — treat as live if near wall clock.
        const skew = Date.now() / 1000 - samples.as_of;
        if (skew < 2) {
          liveWindowEnd = samples.as_of;
          chartAsOf = null;
        } else {
          chartAsOf = samples.as_of;
        }
      }
      syncNavButtons();
      renderSparkline(
        svg,
        tooltip,
        samples.points,
        requestedWindow,
        samples.as_of,
        current.sensor_key,
      );
    } catch (err) {
      if (generation !== chartRequestGeneration) {
        return;
      }
      status.hidden = false;
      status.textContent = `Could not load samples: ${formatError(err)}`;
    }
  };

  const persistConfig = async (): Promise<void> => {
    status.hidden = true;
    const raw = intervalSelect.value;
    const enabled = raw !== SensorCollectionFrequencyChoice.DontCollect;
    const intervalS = enabled
      ? Number(raw)
      : current.interval_s > 0
        ? current.interval_s
        : SensorCollectionIntervalS.Fifteen;
    const next = {
      enabled,
      interval_s: intervalS,
    };
    intervalSelect.disabled = true;
    try {
      current = await dataSource.saveSensorCollectionSensor(
        current.device_id,
        current.sensor_key,
        next,
      );
      syncIntervalSelect(intervalSelect, current.enabled, current.interval_s);
      meta.textContent = formatLastSample(
        current.last_sample_at,
        current.last_value,
        current.unit,
        current.sensor_key,
      );
      showSuccessToast(
        current.enabled
          ? `Collecting ${sensorKeyLabel(current.sensor_key)} every ${current.interval_s}s`
          : `Stopped collecting ${sensorKeyLabel(current.sensor_key)}`,
      );
      await refreshChart();
    } catch (err) {
      status.hidden = false;
      status.textContent = `Could not save: ${formatError(err)}`;
      syncIntervalSelect(intervalSelect, current.enabled, current.interval_s);
    } finally {
      intervalSelect.disabled = false;
    }
  };

  intervalSelect.addEventListener("change", () => {
    void persistConfig();
  });
  windowSelect.addEventListener("change", () => {
    chartWindow = windowSelect.value as SensorChartWindow;
    chartAsOf = null;
    syncNavButtons();
    void refreshChart();
  });
  prevBtn.addEventListener("click", () => {
    const duration = windowDurationS(chartWindow);
    chartAsOf = (chartAsOf ?? lastWindowEnd) - duration;
    syncNavButtons();
    void refreshChart();
  });
  nextBtn.addEventListener("click", () => {
    if (chartAsOf === null) {
      return;
    }
    const duration = windowDurationS(chartWindow);
    const nextEnd = chartAsOf + duration;
    // Snap to live when Next would reach (or pass) the last known live end —
    // comparing to Date.now() fails after a pause because the prior live end
    // has already receded into the past.
    if (nextEnd >= liveWindowEnd - 1) {
      chartAsOf = null;
    } else {
      chartAsOf = nextEnd;
    }
    syncNavButtons();
    void refreshChart();
  });

  void refreshChart();
  return card;
}
