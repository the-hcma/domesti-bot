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

interface PendingPrediction {
  state: UIDeviceState;
  expiresAt: number;
}

class DomestiBotController {
  // Background poll cadence: refreshes ``/v1/ui/state`` so the family
  // frames flip between green (backend reachable) and red (backend
  // unreachable) without the user having to click anything.
  private static readonly POLL_MS = 5000;

  // Grace window during which a click's optimistic prediction
  // overrides contradicting poll results. Picked to comfortably
  // outlast (a) Tailwind's transient ``OPENING`` / ``CLOSING`` (~5-8s
  // typical garage door cycle) and (b) one to two ``POLL_MS`` cycles
  // for slow-to-settle Kasa relays. Expiration is per-device, so a
  // user click resets only that tile's grace window.
  private static readonly OPTIMISTIC_GRACE_MS = 8000;

  private readonly root: HTMLElement;
  private state: UIStateOut | null = null;
  private connected = false;
  private pollTimer: number | null = null;
  // Keyed by ``familyId\u0000deviceId``. Survives across polls so the
  // tile keeps showing the predicted state even when the backend
  // momentarily disagrees (transient OPENING, slow Kasa cloud sync,
  // etc.). Confirmed predictions (poll state == predicted state)
  // delete themselves; expired predictions release their hold.
  private pendingPredictions: Map<string, PendingPrediction> = new Map();

  constructor(root: HTMLElement) {
    this.root = root;
  }

  async init(): Promise<void> {
    this.renderShell("Loading device state…");
    await this.refresh({ showErrorIfNoState: true });
    this.schedulePoll();
  }

  private async onBulkOffFamily(familyId: string): Promise<void> {
    // A bulk command supersedes any single-tile predictions in that
    // family. Replace them with one prediction per non-excluded device
    // pointing at the bulk-off target state ("off" for switches,
    // "closed" for doors) so the tiles flip immediately, the action
    // labels swap to "Turn it on" / "Open it", and the grace window
    // suppresses any contradictory poll readings that arrive while
    // the devices are still settling.
    if (familyId !== "kasa" && familyId !== "tailwind") return;
    this.predictBulkOffForFamily(familyId);
    this.render();
    try {
      if (familyId === "kasa") {
        await api.bulkOffKasa();
      } else {
        await api.closeAllTailwind();
      }
      await this.refresh();
    } catch (err) {
      // Bulk dispatch failed — every prediction we just registered
      // for this family is provably wrong (the request never landed
      // server-side), so drop the grace windows and let the next
      // poll show reality.
      this.clearPendingPredictionsForFamily(familyId);
      this.renderError(`Failed to bulk action on ${familyId}`, err);
    }
  }

  private async onBulkOffGlobal(): Promise<void> {
    // Same optimistic pattern as the per-family bulk-off, but spans
    // every family. Excluded devices keep their current state — the
    // backend won't touch them, so neither should the prediction
    // overlay. On dispatch failure we drop every prediction we just
    // registered so a backend that never received the call doesn't
    // leave the UI lying about device state.
    this.predictBulkOffGlobal();
    this.render();
    try {
      await api.bulkOffGlobal();
      await this.refresh();
    } catch (err) {
      this.clearAllPendingPredictions();
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
      // Action failed → prediction is provably wrong, drop the grace
      // window for this device so the next refresh shows reality.
      this.clearPendingPrediction(device.family_id, device.id);
      console.warn(`[domesti-bot] operate ${device.label} failed`, err);
      await this.refresh();
    }
  }

  private async onToggleKasa(device: UIDeviceOut): Promise<void> {
    // Optimistic update: predict the post-action state and re-render
    // immediately so the button label flips to the *next* action
    // without waiting for the round-trip. The pending prediction
    // (see ``predictDeviceState``) also holds across polls during a
    // grace window so a transient backend disagreement doesn't
    // flicker the label back. On action failure we drop the grace
    // window and refresh so the user sees what really happened.
    const nextOn = device.state !== "on";
    this.predictDeviceState(device.family_id, device.id, nextOn ? "on" : "off");
    this.render();
    try {
      await api.toggleKasa(device.id, nextOn);
    } catch (err) {
      this.clearPendingPrediction(device.family_id, device.id);
      console.warn(`[domesti-bot] toggle ${device.label} failed`, err);
      await this.refresh();
    }
  }

  private applyPendingPredictionsTo(state: UIStateOut): void {
    // After a fresh ``fetchState()`` lands, overlay any still-active
    // optimistic predictions onto the canonical snapshot so the user
    // doesn't see a flicker back to the pre-action label while the
    // device (or the backend's view of it) is settling.
    //
    // Two ways a pending prediction releases its hold:
    //   * the canonical state matches the prediction → confirmed,
    //     drop the entry, let real readings flow through;
    //   * the entry's ``expiresAt`` has passed → grace window over,
    //     drop the entry and trust whatever the backend reports
    //     (this is how a genuinely failed action becomes visible).
    if (this.pendingPredictions.size === 0) return;
    const now = performance.now();
    for (const [key, pending] of Array.from(this.pendingPredictions)) {
      if (pending.expiresAt <= now) this.pendingPredictions.delete(key);
    }
    if (this.pendingPredictions.size === 0) return;
    for (const family of state.families) {
      for (const device of family.devices) {
        const key = DomestiBotController.predictionKey(family.id, device.id);
        const pending = this.pendingPredictions.get(key);
        if (!pending) continue;
        if (device.state === pending.state) {
          this.pendingPredictions.delete(key);
        } else {
          device.state = pending.state;
        }
      }
    }
  }

  private clearPendingPrediction(familyId: string, deviceId: string): void {
    this.pendingPredictions.delete(
      DomestiBotController.predictionKey(familyId, deviceId),
    );
  }

  private clearPendingPredictionsForFamily(familyId: string): void {
    // Bulk family actions invalidate any pending per-tile prediction
    // in that family — the user's latest intent is the bulk command,
    // and the canonical refresh after the bulk should win immediately.
    const prefix = `${familyId}\u0000`;
    for (const key of Array.from(this.pendingPredictions.keys())) {
      if (key.startsWith(prefix)) this.pendingPredictions.delete(key);
    }
  }

  private clearAllPendingPredictions(): void {
    this.pendingPredictions.clear();
  }

  private predictBulkOffForFamily(familyId: string): void {
    // Register an optimistic prediction for every non-excluded device
    // in ``familyId`` that the bulk-off command will actually act on.
    // We deliberately don't touch excluded devices: the backend will
    // skip them, and showing them flipping off would be a lie that
    // the next refresh has to correct.
    if (!this.state) return;
    for (const family of this.state.families) {
      if (family.id !== familyId) continue;
      for (const device of family.devices) {
        if (device.exclude_from_global) continue;
        this.predictDeviceState(
          family.id,
          device.id,
          bulkOffStateForKind(device.kind),
        );
      }
    }
  }

  private predictBulkOffGlobal(): void {
    // Spans every family. Mirrors :meth:`predictBulkOffForFamily` for
    // the global "Turn off / close everything" button.
    if (!this.state) return;
    for (const family of this.state.families) {
      for (const device of family.devices) {
        if (device.exclude_from_global) continue;
        this.predictDeviceState(
          family.id,
          device.id,
          bulkOffStateForKind(device.kind),
        );
      }
    }
  }

  private predictDeviceState(
    familyId: string,
    deviceId: string,
    nextState: UIDeviceState,
  ): void {
    // Two effects, both essential:
    //   1. mutate the cached ``state`` in place so the *immediate*
    //      next ``render()`` shows the predicted state (snappy UI);
    //   2. register a pending prediction with an expiry so the *next
    //      few polls* don't flicker the label back while the device
    //      / backend are settling. See ``applyPendingPredictionsTo``.
    this.pendingPredictions.set(
      DomestiBotController.predictionKey(familyId, deviceId),
      {
        state: nextState,
        expiresAt: performance.now() + DomestiBotController.OPTIMISTIC_GRACE_MS,
      },
    );
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

  private static predictionKey(familyId: string, deviceId: string): string {
    // Use NUL as the separator because neither family nor device ids
    // contain it — keeps the key unambiguous without escape rules.
    return `${familyId}\u0000${deviceId}`;
  }

  private async refresh(
    opts: { showErrorIfNoState?: boolean } = {},
  ): Promise<void> {
    try {
      this.state = await api.fetchState();
      this.connected = true;
      this.applyPendingPredictionsTo(this.state);
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
      globalBtn.textContent = "Turn off / close everything";
      globalBtn.disabled = !this.connected;
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

function bulkOffStateForKind(kind: UIDeviceOut["kind"]): UIDeviceState {
  // What the backend's bulk-off endpoints actually drive each device
  // kind to. Used by the controller's optimistic-prediction helpers
  // so the UI can show the post-action state before the round-trip
  // (and the post-action poll) lands.
  return kind === "switch" ? "off" : "closed";
}

function renderDevice(
  device: UIDeviceOut,
  controller: DomestiBotController,
  connected: boolean,
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
  toggle.disabled = !connected;
  tile.append(toggle);

  const excludeRow = document.createElement("label");
  excludeRow.className = "tile-exclude";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = device.exclude_from_global;
  checkbox.disabled = !connected;
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
    // Per-family bulk-off is always destructive ("Turn off all" /
    // "Close all"), so use the same red ``btn-danger`` styling as the
    // global "Turn off / close everything" button — colors stay consistent
    // with the green/red rule that drives the state badges and the
    // per-tile toggles.
    bulkBtn.className = "btn btn-danger";
    bulkBtn.textContent = family.id === "kasa" ? "Turn off all" : "Close all";
    bulkBtn.disabled = !connected;
    bulkBtn.addEventListener("click", () => {
      controller.bulkActionFamilyTile(family.id);
    });
    header.append(bulkBtn);
  }
  section.append(header);

  const grid = document.createElement("div");
  grid.className = "tile-grid";
  for (const device of family.devices) {
    grid.append(renderDevice(device, controller, connected));
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
