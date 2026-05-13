// Browser entrypoint for the domesti-bot landing page.
//
// PR4 of the `web-ui-tiles` stack: render the kasa tile grid + bulk action
// bar (per-family kasa-all-off, global all-off) and wire the per-tile
// toggle and "exclude from global" checkbox to the FastAPI endpoints.
//
// Tailwind tiles + tailwind-specific bulk actions land in PR5 (the server
// already returns tailwind tiles in `GET /v1/ui/state`; this PR renders
// them as read-only state tiles without a toggle so the family is visible
// without claiming functionality it doesn't have yet).

import { api, HttpError } from "./api.js";
import type { UIDeviceOut, UIFamilyOut, UIStateOut } from "./types.js";

const APP_ROOT_ID = "app";
const STATUS_ID = "bundle-status";

class AppController {
  private readonly root: HTMLElement;
  private state: UIStateOut | null = null;

  constructor(root: HTMLElement) {
    this.root = root;
  }

  async init(): Promise<void> {
    this.renderShell("Loading device state…");
    try {
      this.state = await api.fetchState();
      this.render();
    } catch (err) {
      this.renderError("Failed to load device state", err);
    }
  }

  private async onBulkOffFamily(familyId: string): Promise<void> {
    if (familyId !== "kasa") return;
    try {
      await api.bulkOffKasa();
      await this.refresh();
    } catch (err) {
      this.renderError(`Failed to turn off all ${familyId}`, err);
    }
  }

  private async onBulkOffGlobal(): Promise<void> {
    try {
      await api.bulkOffGlobal();
      await this.refresh();
    } catch (err) {
      this.renderError("Failed to run global all-off", err);
    }
  }

  private async onSetExclude(
    device: UIDeviceOut,
    excludeFromGlobal: boolean,
  ): Promise<void> {
    try {
      await api.setExclude(device.family_id, device.id, excludeFromGlobal);
      await this.refresh();
    } catch (err) {
      this.renderError(
        `Failed to update preference for ${device.label}`,
        err,
      );
    }
  }

  private async onToggleKasa(device: UIDeviceOut): Promise<void> {
    const next = device.state !== "on";
    try {
      await api.toggleKasa(device.id, next);
      await this.refresh();
    } catch (err) {
      this.renderError(`Failed to toggle ${device.label}`, err);
    }
  }

  private async refresh(): Promise<void> {
    try {
      this.state = await api.fetchState();
      this.render();
    } catch (err) {
      this.renderError("Failed to refresh device state", err);
    }
  }

  private render(): void {
    const state = this.state;
    if (!state) return;
    this.root.replaceChildren();
    const header = document.createElement("header");
    header.className = "tile-header";
    const h2 = document.createElement("h2");
    h2.textContent = "Devices";
    header.append(h2);
    if (state.families.length > 0) {
      const globalBtn = document.createElement("button");
      globalBtn.type = "button";
      globalBtn.className = "btn btn-danger";
      globalBtn.textContent = "Turn everything off";
      globalBtn.addEventListener("click", () => {
        void this.onBulkOffGlobal();
      });
      header.append(globalBtn);
    }
    this.root.append(header);

    if (state.families.length === 0) {
      const empty = document.createElement("p");
      empty.className = "tile-empty";
      empty.textContent =
        "No devices discovered yet. The server is still bringing them up — refresh in a few seconds.";
      this.root.append(empty);
      return;
    }

    for (const family of state.families) {
      this.root.append(renderFamily(family, this));
    }
  }

  private renderError(prefix: string, err: unknown): void {
    const detail = err instanceof HttpError
      ? `${err.status}: ${err.bodyText.slice(0, 200)}`
      : err instanceof Error
        ? err.message
        : String(err);
    this.root.replaceChildren();
    const banner = document.createElement("div");
    banner.className = "tile-error";
    banner.textContent = `${prefix} — ${detail}`;
    this.root.append(banner);
    const retry = document.createElement("button");
    retry.type = "button";
    retry.className = "btn";
    retry.textContent = "Retry";
    retry.addEventListener("click", () => {
      void this.init();
    });
    this.root.append(retry);
  }

  private renderShell(message: string): void {
    this.root.replaceChildren();
    const placeholder = document.createElement("p");
    placeholder.className = "tile-loading";
    placeholder.textContent = message;
    this.root.append(placeholder);
  }

  toggleKasaTile(device: UIDeviceOut): void {
    void this.onToggleKasa(device);
  }

  bulkOffFamilyTile(familyId: string): void {
    void this.onBulkOffFamily(familyId);
  }

  setExcludeTile(device: UIDeviceOut, excludeFromGlobal: boolean): void {
    void this.onSetExclude(device, excludeFromGlobal);
  }
}

function markBundleReady(): void {
  const el = document.getElementById(STATUS_ID);
  if (!el) return;
  el.textContent = "loaded";
  el.dataset["state"] = "ready";
}

function renderDevice(
  device: UIDeviceOut,
  controller: AppController,
): HTMLElement {
  const tile = document.createElement("article");
  tile.className = `tile tile-${device.kind}`;
  tile.dataset["familyId"] = device.family_id;
  tile.dataset["deviceId"] = device.id;
  tile.dataset["state"] = device.state;

  const head = document.createElement("div");
  head.className = "tile-head";
  const label = document.createElement("h4");
  label.className = "tile-label";
  label.textContent = device.label;
  head.append(label);

  const stateBadge = document.createElement("span");
  stateBadge.className = `tile-state tile-state-${device.state}`;
  stateBadge.textContent = device.state;
  head.append(stateBadge);
  tile.append(head);

  if (device.kind === "switch") {
    const toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "tile-toggle";
    toggle.dataset["on"] = device.state === "on" ? "true" : "false";
    toggle.setAttribute(
      "aria-pressed",
      device.state === "on" ? "true" : "false",
    );
    toggle.textContent = device.state === "on" ? "On" : "Off";
    toggle.addEventListener("click", () => {
      controller.toggleKasaTile(device);
    });
    tile.append(toggle);
  } else {
    // Door tiles render read-only in PR4; the open/close controls land
    // in PR5 once the tailwind action endpoints exist.
    const note = document.createElement("p");
    note.className = "tile-note";
    note.textContent = "Open / close lands in the next update.";
    tile.append(note);
  }

  const excludeRow = document.createElement("label");
  excludeRow.className = "tile-exclude";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = device.exclude_from_global;
  checkbox.addEventListener("change", () => {
    controller.setExcludeTile(device, checkbox.checked);
  });
  excludeRow.append(checkbox);
  const span = document.createElement("span");
  span.textContent = "Exclude from global all-off";
  excludeRow.append(span);
  tile.append(excludeRow);
  return tile;
}

function renderFamily(
  family: UIFamilyOut,
  controller: AppController,
): HTMLElement {
  const section = document.createElement("section");
  section.className = "family";
  section.dataset["familyId"] = family.id;
  section.style.setProperty("--family-color", family.color);

  const header = document.createElement("header");
  header.className = "family-header";
  const heading = document.createElement("h3");
  heading.textContent = family.label;
  header.append(heading);

  if (family.id === "kasa") {
    const bulkBtn = document.createElement("button");
    bulkBtn.type = "button";
    bulkBtn.className = "btn";
    bulkBtn.textContent = "Turn off all";
    bulkBtn.addEventListener("click", () => {
      controller.bulkOffFamilyTile(family.id);
    });
    header.append(bulkBtn);
  }
  section.append(header);

  const grid = document.createElement("div");
  grid.className = "tile-grid";
  for (const device of family.devices) {
    grid.append(renderDevice(device, controller));
  }
  section.append(grid);
  return section;
}

function start(): void {
  markBundleReady();
  const root = document.getElementById(APP_ROOT_ID);
  if (!root) {
    console.warn(`[domesti-bot] expected #${APP_ROOT_ID} in landing page`);
    return;
  }
  const controller = new AppController(root);
  void controller.init();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
