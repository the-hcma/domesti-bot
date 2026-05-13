// Browser entrypoint for the domesti-bot landing page.
//
// PR5 of the `web-ui-tiles` stack: tailwind tiles get the Open / Close
// controls + the family-level "Close all" button, and the global
// "Turn everything off" now closes every tailwind door alongside turning
// every kasa switch off (honoring per-device `exclude_from_global=True`).
//
// Earlier PRs in this stack:
//   PR1 — TypeScript toolchain (pnpm + esbuild + /static mount)
//   PR2 — `ui_preferences` SQLite table
//   PR3 — `GET /v1/ui/state` read-only endpoint
//   PR4 — kasa tile rendering + per-tile toggle + family bulk-off + global
//         bulk-off + per-tile exclude-from-global checkbox

import { api, HttpError } from "./api.js";
import type { UIDeviceOut, UIFamilyOut, UIStateOut } from "./types.js";

const APP_ROOT_ID = "app";
const STATUS_ID = "bundle-status";

class DomestiBotController {
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
    try {
      if (familyId === "kasa") {
        await api.bulkOffKasa();
      } else if (familyId === "tailwind") {
        await api.closeAllTailwind();
      } else {
        return;
      }
      await this.refresh();
    } catch (err) {
      this.renderError(`Failed to bulk action on ${familyId}`, err);
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

  private async onOperateTailwind(device: UIDeviceOut): Promise<void> {
    // Symmetric with onToggleKasa: derive the next state from the current
    // one. ``unknown`` (transient ``OPENING`` / ``CLOSING``) defaults to
    // closing — safer than opening unattended.
    const wantOpen = device.state === "closed";
    try {
      if (wantOpen) {
        await api.openTailwindDoor(device.id);
      } else {
        await api.closeTailwindDoor(device.id);
      }
      await this.refresh();
    } catch (err) {
      this.renderError(`Failed to operate ${device.label}`, err);
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

  operateTailwindTile(device: UIDeviceOut): void {
    void this.onOperateTailwind(device);
  }

  bulkActionFamilyTile(familyId: string): void {
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
  controller: DomestiBotController,
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
    // Door tile: a single button that flips between Open and Close based
    // on the current cached position. ``unknown`` (transient ``OPENING``
    // / ``CLOSING``) renders as Close — same default as the controller.
    const op = document.createElement("button");
    op.type = "button";
    op.className = "tile-toggle";
    const isOpen = device.state === "open";
    op.dataset["on"] = isOpen ? "true" : "false";
    op.setAttribute("aria-pressed", isOpen ? "true" : "false");
    op.textContent = isOpen ? "Close" : "Open";
    op.addEventListener("click", () => {
      controller.operateTailwindTile(device);
    });
    tile.append(op);
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
  controller: DomestiBotController,
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

  if (family.id === "kasa" || family.id === "tailwind") {
    const bulkBtn = document.createElement("button");
    bulkBtn.type = "button";
    bulkBtn.className = "btn";
    bulkBtn.textContent = family.id === "kasa" ? "Turn off all" : "Close all";
    bulkBtn.addEventListener("click", () => {
      controller.bulkActionFamilyTile(family.id);
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
  const controller = new DomestiBotController(root);
  void controller.init();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
