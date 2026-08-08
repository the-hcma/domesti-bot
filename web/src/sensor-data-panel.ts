// Automations → Data: per-sensor collection controls + time-window charts.

import { HttpError } from "./api.js";
import {
  SensorChartWindow,
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

function formatAxisTime(epochS: number, window: SensorChartWindow): string {
  const when = new Date(epochS * 1000);
  if (window === SensorChartWindow.LastDay) {
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
): string {
  if (at === null || value === null) {
    return "No samples yet";
  }
  const when = new Date(at * 1000).toLocaleString();
  const unitSuffix = unit === null || unit === "" ? "" : ` ${unit}`;
  return `Last: ${value}${unitSuffix} · ${when}`;
}

function formatSampleTooltip(point: SensorCollectionSampleOut): string {
  const unitSuffix =
    point.unit === null || point.unit === "" ? "" : ` ${point.unit}`;
  const when = new Date(point.recorded_at * 1000).toLocaleString();
  return `${formatAxisReading(point.value)}${unitSuffix} · ${when}`;
}

function renderSparkline(
  svg: SVGSVGElement,
  tooltip: HTMLElement,
  points: SensorCollectionSampleOut[],
  window: SensorChartWindow,
  asOf: number,
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

  const plotLeft = CHART_PAD_LEFT;
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
  const values = points.map((p) => p.value);
  let minV = Math.min(...values);
  let maxV = Math.max(...values);
  if (minV === maxV) {
    minV -= 1;
    maxV += 1;
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

  const yTicks = [minV, (minV + maxV) / 2, maxV];
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
    label.textContent = formatAxisReading(tick);
    axis.append(grid, label);
  }

  const xTicks =
    chartWidth < 420 || window === SensorChartWindow.LastDay
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
    tipText: formatSampleTooltip(point),
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
    "Collect sensor readings into SQLite and inspect them on a chart. Enable a sensor, pick a sample frequency, then choose a time window.";

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

function buildSensorCard(
  initial: SensorCollectionSensorOut,
  dataSource: RulesDataSource,
  status: HTMLParagraphElement,
): HTMLElement {
  let current = initial;
  let chartWindow: SensorChartWindow = SensorChartWindow.Last5Minutes;

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
  );

  const controls = document.createElement("div");
  controls.className = "rules-sensor-controls";

  const enabledLabel = document.createElement("label");
  enabledLabel.className = "rules-sensor-enabled";
  const enabledCb = document.createElement("input");
  enabledCb.type = "checkbox";
  enabledCb.checked = current.enabled;
  enabledLabel.append(enabledCb, document.createTextNode(" Collect"));

  const intervalField = document.createElement("div");
  intervalField.className = "settings-dialog-field";
  intervalField.append(createFieldLabel("Frequency"));
  const intervalSelect = document.createElement("select");
  for (const opt of INTERVAL_OPTIONS) {
    const option = document.createElement("option");
    option.value = String(opt.value);
    option.textContent = opt.label;
    if (opt.value === current.interval_s) {
      option.selected = true;
    }
    intervalSelect.append(option);
  }
  intervalField.append(intervalSelect);

  const windowField = document.createElement("div");
  windowField.className = "settings-dialog-field";
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

  controls.append(enabledLabel, intervalField, windowField);

  const chartWrap = document.createElement("div");
  chartWrap.className = "rules-sensor-chart-wrap";
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("rules-sensor-chart");
  const tooltip = document.createElement("div");
  tooltip.className = "rules-sensor-chart-tooltip";
  tooltip.hidden = true;
  tooltip.setAttribute("role", "tooltip");
  chartWrap.append(svg, tooltip);

  card.append(title, meta, controls, chartWrap);

  let chartRequestGeneration = 0;

  const refreshChart = async (): Promise<void> => {
    const generation = ++chartRequestGeneration;
    const requestedWindow = chartWindow;
    try {
      const samples = await dataSource.getSensorCollectionSamples(
        current.device_id,
        current.sensor_key,
        requestedWindow,
      );
      if (generation !== chartRequestGeneration) {
        return;
      }
      renderSparkline(svg, tooltip, samples.points, requestedWindow, samples.as_of);
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
    const next = {
      enabled: enabledCb.checked,
      interval_s: Number(intervalSelect.value),
    };
    enabledCb.disabled = true;
    intervalSelect.disabled = true;
    try {
      current = await dataSource.saveSensorCollectionSensor(
        current.device_id,
        current.sensor_key,
        next,
      );
      enabledCb.checked = current.enabled;
      intervalSelect.value = String(current.interval_s);
      meta.textContent = formatLastSample(
        current.last_sample_at,
        current.last_value,
        current.unit,
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
      enabledCb.checked = current.enabled;
      intervalSelect.value = String(current.interval_s);
    } finally {
      enabledCb.disabled = false;
      intervalSelect.disabled = false;
    }
  };

  enabledCb.addEventListener("change", () => {
    void persistConfig();
  });
  intervalSelect.addEventListener("change", () => {
    void persistConfig();
  });
  windowSelect.addEventListener("change", () => {
    chartWindow = windowSelect.value as SensorChartWindow;
    void refreshChart();
  });

  void refreshChart();
  return card;
}
