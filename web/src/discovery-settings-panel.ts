// Settings → Device Discovery: cache-first explanation + LAN rediscover.

import { api, HttpError } from "./api.js";
import { ToastVariant } from "./closed-sets.js";
import {
  clearSettingsDialogStatus,
  setSettingsDialogStatus,
} from "./settings-status.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  DiscoveryFamilyStatusOut,
  DiscoveryRefreshOut,
  DiscoverySettingsOut,
} from "./types.js";

export const DISCOVERY_SETTINGS_LEAD =
  "Startup reconnects known devices from the discovery cache so the server comes up quickly. Brand-new plugs, speakers, and doors on the LAN are not found until you refresh discovery (same as the CLI refresh-discovery command).";

export const DISCOVERY_SETTINGS_REFRESH_LABEL = "Refresh discovery";

export const DISCOVERY_SETTINGS_REFRESHING = "Refreshing discovery across all device families…";

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

  const newDevicesHeading = document.createElement("h3");
  newDevicesHeading.className = "settings-dialog-subheading";
  newDevicesHeading.textContent = "New devices from last refresh";
  newDevicesHeading.hidden = true;

  const newDevicesList = document.createElement("ul");
  newDevicesList.className = "discovery-settings-new-devices";
  newDevicesList.hidden = true;

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const refreshBtn = document.createElement("button");
  refreshBtn.type = "button";
  refreshBtn.className = "btn";
  refreshBtn.textContent = DISCOVERY_SETTINGS_REFRESH_LABEL;
  actions.append(refreshBtn);

  form.append(status, familyList, newDevicesHeading, newDevicesList, actions);
  container.append(form);

  const showStatus = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(status, message, tone);
  };

  const renderFamilies = (families: readonly DiscoveryFamilyStatusOut[]): void => {
    familyList.replaceChildren();
    for (const family of families) {
      const item = document.createElement("li");
      item.className = "discovery-settings-family-row";
      if (!family.available) {
        item.textContent = `${family.label}: not loaded`;
      } else {
        const source =
          family.last_discovery_source === "cache"
            ? "cache"
            : family.last_discovery_source === "discovery"
              ? "LAN discovery"
              : "source unknown";
        item.textContent = `${family.label}: ${family.device_count} device(s) (${source})`;
      }
      familyList.append(item);
    }
  };

  const renderNewDevices = (result: DiscoveryRefreshOut): void => {
    const devices = result.new_devices;
    if (devices.length === 0) {
      newDevicesHeading.hidden = true;
      newDevicesList.hidden = true;
      newDevicesList.replaceChildren();
      return;
    }
    newDevicesHeading.hidden = false;
    newDevicesList.hidden = false;
    newDevicesList.replaceChildren();
    for (const device of devices) {
      const item = document.createElement("li");
      item.textContent = device.display;
      newDevicesList.append(item);
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
      showStatus(DISCOVERY_SETTINGS_REFRESHING, ToastVariant.Info);
      try {
        const result = await api.refreshDiscovery();
        renderFamilies(
          result.families.map((family) => ({
            available: !family.skipped,
            device_count: family.device_count,
            family_id: family.family_id,
            label: family.label,
            last_discovery_source: family.source,
          })),
        );
        renderNewDevices(result);
        const newCount = result.new_devices.length;
        const failed = result.families.filter((f) => !f.ok && !f.skipped);
        if (failed.length > 0) {
          const detail = `Discovery finished with ${failed.length} family error(s).`;
          showStatus(detail, ToastVariant.Error);
          showErrorToast(detail);
        } else if (newCount > 0) {
          const detail =
            newCount === 1
              ? `Discovery refreshed. New device found: ${result.new_devices[0]?.display ?? ""}`
              : `Discovery refreshed. ${newCount} new devices found.`;
          showStatus(detail, ToastVariant.Success);
          showSuccessToast(detail);
        } else {
          const detail = "Discovery refreshed. No new devices found.";
          showStatus(detail, ToastVariant.Success);
          showSuccessToast(detail);
        }
        await options.onDevicesChanged?.();
      } catch (err) {
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

function formatDiscoveryError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail || err.message;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}
