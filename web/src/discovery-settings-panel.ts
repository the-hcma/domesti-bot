// Settings → Device Discovery: cache-first explanation + LAN rediscover.

import { api, HttpError } from "./api.js";
import { ToastVariant } from "./closed-sets.js";
import {
  clearSettingsDialogStatus,
  setSettingsDialogStatus,
} from "./settings-status.js";
import {
  dismissActiveToast,
  showDiscoveryProgressToast,
  showErrorToast,
  showSuccessToast,
} from "./ui-toast.js";
import type {
  DiscoveryDeviceOut,
  DiscoveryFamilyStatusOut,
  DiscoveryRefreshOut,
  DiscoverySettingsOut,
} from "./types.js";

export const DISCOVERY_SETTINGS_LEAD =
  "Startup reconnects known devices from the discovery cache so the server comes up quickly. Brand-new plugs, speakers, and doors on the LAN are not found until you refresh discovery (same as the CLI refresh-discovery command).";

export const DISCOVERY_SETTINGS_REFRESH_LABEL = "Refresh discovery";

export const DISCOVERY_PROGRESS_TOAST_MESSAGE =
  "Discovering devices on the LAN…";

export const DISCOVERY_REFRESH_NO_NEW_DEVICES =
  "Discovery refreshed. No new devices found.";

export const DISCOVERY_NEW_DEVICE_BADGE = "New";

const DISCOVERY_LAST_DURATION_KEY = "domesti-discovery-last-ms";
const DISCOVERY_ESTIMATE_DEFAULT_MS = 30_000;
const DISCOVERY_ESTIMATE_MIN_MS = 5_000;
const DISCOVERY_ESTIMATE_MAX_MS = 90_000;

type FamilyRow = DiscoveryFamilyStatusOut & { error?: string | null };

export async function mountDiscoverySettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "discovery-settings-form";
  form.noValidate = true;

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent = DISCOVERY_SETTINGS_LEAD;
  form.append(lead);

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const familyList = document.createElement("ul");
  familyList.className = "discovery-settings-family-list";
  familyList.setAttribute("aria-label", "Discovery status by family");

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const refreshBtn = document.createElement("button");
  refreshBtn.type = "button";
  refreshBtn.className = "btn";
  refreshBtn.textContent = DISCOVERY_SETTINGS_REFRESH_LABEL;
  actions.append(refreshBtn);

  form.append(status, familyList, actions);
  container.append(form);

  let highlightedNewKeys = new Set<string>();

  const showStatus = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(status, message, tone);
  };

  const renderFamilies = (families: readonly FamilyRow[]): void => {
    familyList.replaceChildren();
    for (const family of families) {
      const item = document.createElement("li");
      item.className = "discovery-settings-family-row";

      const heading = document.createElement("div");
      heading.className = "discovery-settings-family-heading";
      if (!family.available) {
        heading.textContent = `${family.label}: not loaded`;
      } else if (family.error) {
        heading.textContent =
          `${family.label}: ${family.device_count} device(s) — refresh failed: ${family.error}`;
      } else {
        const sourceLabel = formatDiscoverySourceLabel(
          family.family_id,
          family.last_discovery_source,
        );
        const sourceSuffix = sourceLabel ? ` (${sourceLabel})` : "";
        heading.textContent =
          `${family.label}: ${family.device_count} device(s)${sourceSuffix}`;
      }
      item.append(heading);

      if (family.available && family.devices.length > 0) {
        const deviceList = document.createElement("ul");
        deviceList.className = "discovery-settings-device-list";
        for (const device of family.devices) {
          deviceList.append(
            buildDeviceListItem(family.family_id, device, highlightedNewKeys),
          );
        }
        item.append(deviceList);
      }

      familyList.append(item);
    }
  };

  const loadStatus = async (): Promise<void> => {
    clearSettingsDialogStatus(status);
    try {
      const payload: DiscoverySettingsOut = await api.fetchDiscoverySettings();
      renderFamilies(payload.families);
    } catch (err) {
      const detail = formatDiscoveryError(err);
      showStatus(detail, ToastVariant.Error);
      showErrorToast(detail);
    }
  };

  refreshBtn.addEventListener("click", () => {
    void (async () => {
      refreshBtn.disabled = true;
      clearSettingsDialogStatus(status);
      const estimatedMs = readDiscoveryDurationEstimate();
      const started = performance.now();
      const endProgressToast = showDiscoveryProgressToast(
        DISCOVERY_PROGRESS_TOAST_MESSAGE,
        estimatedMs,
      );
      try {
        const result = await api.refreshDiscovery();
        storeDiscoveryDurationEstimate(performance.now() - started);
        endProgressToast();
        highlightedNewKeys = collectNewDeviceKeys(result);
        renderFamilies(mapRefreshFamilies(result));
        const newCount = result.new_devices.length;
        const failed = result.families.filter((f) => !f.ok && !f.skipped);
        if (failed.length > 0) {
          const detail = failed
            .map((f) => `${f.label}: ${f.error ?? "rediscover failed"}`)
            .join("; ");
          showStatus(`Discovery finished with errors — ${detail}`, ToastVariant.Error);
          showErrorToast(`Discovery finished with errors — ${detail}`);
        } else if (newCount > 0) {
          const detail =
            newCount === 1
              ? `Discovery refreshed. New device found: ${result.new_devices[0]?.display ?? ""}`
              : `Discovery refreshed. ${newCount} new devices found.`;
          showStatus(detail, ToastVariant.Success);
          showSuccessToast(detail);
        } else {
          showStatus(DISCOVERY_REFRESH_NO_NEW_DEVICES, ToastVariant.Success);
          showSuccessToast(DISCOVERY_REFRESH_NO_NEW_DEVICES);
        }
        await options.onDevicesChanged?.();
      } catch (err) {
        endProgressToast();
        dismissActiveToast();
        const detail = formatDiscoveryError(err);
        showStatus(detail, ToastVariant.Error);
        showErrorToast(detail);
      } finally {
        refreshBtn.disabled = false;
      }
    })();
  });

  await loadStatus();
}

function buildDeviceListItem(
  familyId: string,
  device: DiscoveryDeviceOut,
  highlightedNewKeys: ReadonlySet<string>,
): HTMLLIElement {
  const item = document.createElement("li");
  const isNew = highlightedNewKeys.has(deviceHighlightKey(familyId, device.device_id));
  item.className = isNew
    ? "discovery-settings-device discovery-settings-device-new"
    : "discovery-settings-device";
  const label = document.createElement("span");
  label.className = "discovery-settings-device-label";
  label.textContent = device.display;
  item.append(label);
  if (isNew) {
    const badge = document.createElement("span");
    badge.className = "discovery-settings-device-new-badge";
    badge.setAttribute("aria-label", DISCOVERY_NEW_DEVICE_BADGE);
    badge.textContent = DISCOVERY_NEW_DEVICE_BADGE;
    item.append(badge);
  }
  return item;
}

function collectNewDeviceKeys(result: DiscoveryRefreshOut): Set<string> {
  const keys = new Set<string>();
  for (const family of result.families) {
    for (const device of family.new_devices) {
      keys.add(deviceHighlightKey(family.family_id, device.device_id));
    }
  }
  return keys;
}

function deviceHighlightKey(familyId: string, deviceId: string): string {
  return `${familyId}\u0000${deviceId}`;
}

function formatDiscoveryError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail || err.message;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

export function formatDiscoverySourceLabel(
  familyId: string,
  source: string | null,
): string | null {
  if (source === "cache") {
    return familyId === "gotailwind" ? "cached hub" : "cache";
  }
  if (source === "discovery") {
    return familyId === "gotailwind" ? "LAN hub lookup" : "LAN discovery";
  }
  return null;
}

function mapRefreshFamilies(result: DiscoveryRefreshOut): FamilyRow[] {
  return result.families.map((family) => ({
    available: !family.skipped,
    device_count: family.device_count,
    devices: family.devices,
    error: family.error,
    family_id: family.family_id,
    label: family.label,
    last_discovery_source: family.source,
  }));
}

function readDiscoveryDurationEstimate(): number | null {
  try {
    const raw = sessionStorage.getItem(DISCOVERY_LAST_DURATION_KEY);
    if (raw === null) {
      return DISCOVERY_ESTIMATE_DEFAULT_MS;
    }
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
      return DISCOVERY_ESTIMATE_DEFAULT_MS;
    }
    return Math.min(
      DISCOVERY_ESTIMATE_MAX_MS,
      Math.max(DISCOVERY_ESTIMATE_MIN_MS, parsed),
    );
  } catch {
    return DISCOVERY_ESTIMATE_DEFAULT_MS;
  }
}

function storeDiscoveryDurationEstimate(durationMs: number): void {
  if (!Number.isFinite(durationMs) || durationMs <= 0) {
    return;
  }
  try {
    sessionStorage.setItem(
      DISCOVERY_LAST_DURATION_KEY,
      String(Math.round(durationMs)),
    );
  } catch {
    // sessionStorage unavailable — skip estimate persistence
  }
}
