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

const CHART_HEIGHT = 72;
const CHART_WIDTH = 280;
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

function renderSparkline(
  svg: SVGSVGElement,
  points: SensorCollectionSampleOut[],
  window: SensorChartWindow,
  asOf: number,
): void {
  while (svg.firstChild !== null) {
    svg.removeChild(svg.firstChild);
  }
  svg.setAttribute("viewBox", `0 0 ${CHART_WIDTH} ${CHART_HEIGHT}`);
  svg.setAttribute("role", "img");
  const empty = document.createElementNS("http://www.w3.org/2000/svg", "text");
  empty.setAttribute("x", String(CHART_WIDTH / 2));
  empty.setAttribute("y", String(CHART_HEIGHT / 2));
  empty.setAttribute("text-anchor", "middle");
  empty.setAttribute("dominant-baseline", "middle");
  empty.setAttribute("class", "rules-sensor-chart-empty");
  if (points.length === 0) {
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
  const padY = 6;
  const usableH = CHART_HEIGHT - padY * 2;
  const coords = points.map((point) => {
    const x =
      ((Math.max(start, Math.min(end, point.recorded_at)) - start) / durationS) *
      CHART_WIDTH;
    const y =
      padY +
      usableH -
      ((point.value - minV) / (maxV - minV)) * usableH;
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

  fieldset.append(help, unlimitedLabel, daysField, saveBtn);
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
  chartWrap.append(svg);

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
      renderSparkline(svg, samples.points, requestedWindow, samples.as_of);
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
