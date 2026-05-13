// Browser entrypoint for the domesti-bot landing page.
//
// Hydrates the empty ``<div id="app">`` in ``app/api/static/index.html``
// with the tile UI: one family-section per device family, one tile per
// device, plus per-family and global bulk actions.

import { api, HttpError } from "./api.js";
import type {
  UIDeviceOut,
  UIDeviceState,
  UIFamilyOut,
  UIStateOut,
} from "./types.js";

const APP_ROOT_ID = "app";

class DomestiBotController {
  // Background poll cadence: refreshes ``/v1/ui/state`` so the family
  // frames flip between green (backend reachable) and red (backend
  // unreachable) without the user having to click anything.
  private static readonly POLL_MS = 5000;

  private readonly root: HTMLElement;
  private state: UIStateOut | null = null;
  private connected = false;
  private pollTimer: number | null = null;

  constructor(root: HTMLElement) {
    this.root = root;
  }

  async init(): Promise<void> {
    this.renderShell("Loading device state…");
    await this.refresh({ showErrorIfNoState: true });
    this.schedulePoll();
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
    // ``unknown`` (transient OPENING/CLOSING) defaults to closing — same
    // safer-default the backend applies. The optimistic prediction
    // matches the action we'll actually send.
    const wantOpen = device.state === "closed";
    const nextState = wantOpen ? "open" : "closed";
    this.predictDeviceState(device.family_id, device.id, nextState);
    this.render();
    try {
      if (wantOpen) {
        await api.openTailwindDoor(device.id);
      } else {
        await api.closeTailwindDoor(device.id);
      }
    } catch (err) {
      console.warn(`[domesti-bot] operate ${device.label} failed`, err);
      await this.refresh();
    }
  }

  private async onToggleKasa(device: UIDeviceOut): Promise<void> {
    // Optimistic update: predict the post-action state and re-render
    // immediately so the button label flips to the *next* action
    // without waiting for the round-trip. The continuous state
    // watcher (see ``app/device_state_watcher.py``) will reconcile
    // with the canonical reading on its next poll; on action failure
    // we fall back to a full refresh.
    const nextOn = device.state !== "on";
    this.predictDeviceState(device.family_id, device.id, nextOn ? "on" : "off");
    this.render();
    try {
      await api.toggleKasa(device.id, nextOn);
    } catch (err) {
      console.warn(`[domesti-bot] toggle ${device.label} failed`, err);
      await this.refresh();
    }
  }

  private predictDeviceState(
    familyId: string,
    deviceId: string,
    nextState: UIDeviceState,
  ): void {
    // Mutate the controller's cached ``state`` in place so the next
    // ``render()`` reflects the predicted device state. No-op when the
    // device isn't found (the watcher's next poll will reconcile).
    if (!this.state) return;
    for (const family of this.state.families) {
      if (family.id !== familyId) continue;
      for (const device of family.devices) {
        if (device.id === deviceId) {
          device.state = nextState;
          return;
        }
      }
    }
  }

  private async refresh(
    opts: { showErrorIfNoState?: boolean } = {},
  ): Promise<void> {
    try {
      this.state = await api.fetchState();
      this.connected = true;
      this.render();
    } catch (err) {
      this.connected = false;
      if (this.state) {
        // We have a cached snapshot — keep the tiles up and just flip
        // the family frames to red so the user sees the backend went
        // away. The poll will recover automatically.
        this.render();
      } else if (opts.showErrorIfNoState) {
        this.renderError("Failed to load device state", err);
      }
    }
  }

  private schedulePoll(): void {
    if (this.pollTimer !== null) return;
    this.pollTimer = window.setInterval(() => {
      void this.refresh();
    }, DomestiBotController.POLL_MS);
  }

  private render(): void {
    const state = this.state;
    if (!state) return;
    this.root.replaceChildren();
    this.root.dataset["connected"] = this.connected ? "true" : "false";
    if (state.families.length > 0) {
      const header = document.createElement("header");
      header.className = "tile-header";
      const globalBtn = document.createElement("button");
      globalBtn.type = "button";
      globalBtn.className = "btn btn-danger";
      globalBtn.textContent = "Turn everything off";
      globalBtn.addEventListener("click", () => {
        void this.onBulkOffGlobal();
      });
      header.append(globalBtn);
      this.root.append(header);
    } else {
      const empty = document.createElement("p");
      empty.className = "tile-empty";
      empty.textContent =
        "No devices discovered yet. The server is still bringing them up — refresh in a few seconds.";
      this.root.append(empty);
      return;
    }

    for (const family of state.families) {
      this.root.append(renderFamily(family, this, this.connected));
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

  // The button label is the *action* the user will get by clicking —
  // derived from the current state. The state badge above already
  // shows the current state; the button shows what'll happen next so
  // the user doesn't have to translate "On" → "click to turn off" in
  // their head. For doors, ``unknown`` (transient OPENING/CLOSING) is
  // treated as not-open so the next click closes — same default as the
  // controller's action handler.
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "tile-toggle";
  let isActive: boolean;
  let actionLabel: string;
  let excludeText: string;
  if (device.kind === "switch") {
    isActive = device.state === "on";
    actionLabel = isActive ? "Turn it off" : "Turn it on";
    excludeText = "Exclude from global all-off";
    toggle.addEventListener("click", () => {
      controller.toggleKasaTile(device);
    });
  } else {
    isActive = device.state === "open";
    actionLabel = isActive ? "Close it" : "Open it";
    excludeText = "Exclude from global close-all";
    toggle.addEventListener("click", () => {
      controller.operateTailwindTile(device);
    });
  }
  toggle.dataset["on"] = isActive ? "true" : "false";
  toggle.setAttribute("aria-pressed", isActive ? "true" : "false");
  toggle.textContent = actionLabel;
  tile.append(toggle);

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
  span.textContent = excludeText;
  excludeRow.append(span);
  tile.append(excludeRow);
  return tile;
}

function renderFamily(
  family: UIFamilyOut,
  controller: DomestiBotController,
  connected: boolean,
): HTMLElement {
  const section = document.createElement("section");
  section.className = "family";
  section.dataset["familyId"] = family.id;
  section.dataset["connected"] = connected ? "true" : "false";
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
