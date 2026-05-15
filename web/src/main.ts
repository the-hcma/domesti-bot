// Browser entrypoint for the domesti-bot landing page.
//
// Hydrates the empty ``<div id="app">`` in ``app/api/static/index.html``
// with the tile UI: one family-section per device family, one tile per
// device, plus per-family and global bulk actions.

import { api, HttpError } from "./api.js";
import type {
  MetaOut,
  TailwindTokenSettingsOut,
  UIDeviceOut,
  UIDeviceState,
  UIFamilyOut,
  UIStateOut,
} from "./types.js";

const APP_ROOT_ID = "app";

const PWA_INSTALL_DISMISS_PERMANENT_KEY = "domesti-pwa-install-dismiss-permanent";
const PWA_INSTALL_DISMISS_SESSION_KEY = "domesti-pwa-install-dismiss-session";

const THEME_STORAGE_KEY = "domesti-color-theme";

/** Moon icon — shown when UI is light (control switches to dark). */
const THEME_GLYPH_MOON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

/** Sun icon — shown when UI is dark (control switches to light). */
const THEME_GLYPH_SUN_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';

/** Eye — token hidden (click to reveal). */
const TOKEN_REVEAL_EYE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

/** Eye off — token visible (click to hide). */
const TOKEN_REVEAL_EYE_OFF_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';

let themeToggleSingleton: HTMLButtonElement | null = null;

/** Chromium-only: deferred until the user taps our Install control. */
interface PwaBeforeInstallPromptEvent extends Event {
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
  prompt: () => Promise<void>;
}

/** Public source repository (tooltip copy + icon link target). */
const DOMESTI_BOT_REPO_HREF = "https://github.com/the-hcma/domesti-bot";

/** Tailwind web dashboard (Local Control Key / token). */
const TAILWIND_WEB_DASHBOARD_HREF = "https://web.gotailwind.com";

const SETTINGS_TOAST_MS = 5000;

let domestiUiController: DomestiBotController | null = null;
let settingsToast: HTMLDivElement | null = null;
let settingsToastTimer: number | null = null;

interface PendingPrediction {
  state: UIDeviceState;
  expiresAt: number;
}

class DomestiBotController {
  // Background poll cadence: refreshes ``/v1/ui/state`` so the family
  // frames flip between green (backend reachable) and red (backend
  // unreachable) without the user having to click anything. Three seconds
  // is a compromise between snappy tiles and LAN / server load.
  private static readonly POLL_MS = 3000;

  // Grace window during which a click's optimistic prediction
  // overrides contradicting poll results. Picked to comfortably
  // outlast (a) Tailwind's transient ``OPENING`` / ``CLOSING`` (~5-8s
  // typical garage door cycle) and (b) one to two ``POLL_MS`` cycles
  // for slow-to-settle Kasa relays. Expiration is per-device, so a
  // user click resets only that tile's grace window.
  private static readonly OPTIMISTIC_GRACE_MS = 8000;

  // Bootstrap retry cadence + deadline used while ``/v1/ui/state``
  // is still answering 503 "Device discovery still in progress".
  // Matches the server's ``Retry-After: 2`` hint so we don't poll
  // faster than the suggested pace. The deadline is a safety belt:
  // a healthy cold-cache discovery resolves within 20-35s, but
  // exotic LAN conditions (a stuck mDNS reflector, a dead Tailwind
  // mDNS browse) can drag it out. After the deadline the user sees
  // the error banner so a wedged server doesn't masquerade as a
  // pretty spinner forever.
  private static readonly BOOTSTRAP_RETRY_MS = 2000;
  private static readonly BOOTSTRAP_DEADLINE_MS = 90000;

  // How long a recoverable action error (e.g. Sonos 409 "queue is
  // empty") stays visible before auto-dismissing. Long enough to
  // read a one-sentence hint, short enough not to overstay if the
  // user has already moved on. A subsequent action error replaces
  // the current toast immediately and restarts the timer; clicking
  // the ``×`` button dismisses on demand.
  private static readonly ACTION_ERROR_TOAST_MS = 10000;

  private readonly root: HTMLElement;
  private state: UIStateOut | null = null;
  private connected = false;
  private pollTimer: number | null = null;
  // The recoverable-action toast lives outside ``#app`` so it
  // survives the ``replaceChildren()`` calls inside ``render()``.
  // ``null`` means no toast is currently mounted; the timer is
  // cleared whenever we dismiss or replace the toast so we don't
  // accidentally remove a *newer* toast when an older one's timer
  // fires.
  private actionErrorToast: HTMLDivElement | null = null;
  private actionErrorTimer: number | null = null;
  // Keyed by ``familyId\u0000deviceId``. Survives across polls so the
  // tile keeps showing the predicted state even when the backend
  // momentarily disagrees (transient OPENING, slow Kasa cloud sync,
  // etc.). Confirmed predictions (poll state == predicted state)
  // delete themselves; expired predictions release their hold.
  private pendingPredictions: Map<string, PendingPrediction> = new Map();
  private meta: MetaOut | null = null;

  constructor(root: HTMLElement) {
    this.root = root;
  }

  async init(): Promise<void> {
    void this.loadMeta();
    // Show a spinner while ``/v1/ui/state`` is still answering 503
    // "still in progress" — the FastAPI lifespan defers device
    // discovery so the HTTP server is up well before the first
    // payload is available. Polling continues every
    // ``BOOTSTRAP_RETRY_MS`` (matches the server's ``Retry-After``
    // hint) until we either have state, hit a non-transient error,
    // or run past ``BOOTSTRAP_DEADLINE_MS``.
    this.renderLoading("Discovering devices…");
    await this.bootstrap();
    this.schedulePoll();
    this.registerVisibilityPollBoost();
  }

  private async bootstrap(): Promise<void> {
    const deadline =
      performance.now() + DomestiBotController.BOOTSTRAP_DEADLINE_MS;
    while (true) {
      try {
        this.state = await api.fetchState();
        this.connected = true;
        this.applyPendingPredictionsTo(this.state);
        this.render();
        return;
      } catch (err) {
        if (err instanceof HttpError && err.isDiscoveryInProgress()) {
          if (performance.now() >= deadline) {
            this.renderError(
              "Device discovery is taking longer than expected",
              err,
            );
            return;
          }
          // Keep the spinner up — DOM is already rendered, no need
          // to repaint every tick. Just wait and retry.
          await DomestiBotController.sleep(
            DomestiBotController.BOOTSTRAP_RETRY_MS,
          );
          continue;
        }
        // Any other error (auth, 500, network, 503 with a different
        // detail like "Device discovery failed: ...") is permanent
        // from the bootstrap's point of view; surface it so the user
        // can read what went wrong.
        this.renderError("Failed to load device state", err);
        return;
      }
    }
  }

  private async loadMeta(): Promise<void> {
    try {
      this.meta = await api.fetchMeta();
      if (this.state) {
        this.render();
      } else if (this.root.querySelector(".tile-loading-row") !== null) {
        this.renderLoading("Discovering devices…");
      }
    } catch {
      this.meta = null;
    }
  }

  private async onBulkOffFamily(familyId: string): Promise<void> {
    // A bulk command supersedes any single-tile predictions in that
    // family. Replace them with one prediction per non-excluded device
    // pointing at the bulk-off target state ("off" for switches,
    // "closed" for doors) so the tiles flip immediately, the action
    // labels swap to "Turn it on" / "Open it", and the grace window
    // suppresses any contradictory poll readings that arrive while
    // the devices are still settling.
    if (
      familyId !== "kasa" &&
      familyId !== "sonos" &&
      familyId !== "tailwind"
    ) {
      return;
    }
    this.predictBulkOffForFamily(familyId);
    this.render();
    try {
      if (familyId === "kasa") {
        await api.bulkOffKasa();
      } else if (familyId === "sonos") {
        await api.pauseAllSonos();
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

  private async onToggleSonos(device: UIDeviceOut): Promise<void> {
    // Symmetric to ``onToggleKasa`` but for Sonos zones. ``unknown``
    // (zone we haven't polled yet, or a stopped queue) defaults to
    // ``Resume it`` so the click is meaningful; if the zone is
    // actually stopped, the SoCo ``play`` call is a no-op rather than
    // an error. Optimistic prediction follows the action: ``paused``
    // → predict ``playing``, anything else → predict ``paused``.
    //
    // 409 Conflict from the server means Sonos refused the
    // transition (UPnP 701 — typically an empty queue: there's
    // nothing to resume). The server has already refreshed the
    // zone's cached ``is_playing`` from a live UPnP read before the
    // 409 escaped, so a follow-up ``refresh()`` will show the tile
    // in its real state. Beyond that, the user clicked something
    // that didn't work — surface the server-side detail in the
    // action-error toast so they understand *why* (and what to do
    // about it: "Pick something to play from the Sonos app first").
    const nextPlaying = device.state !== "playing";
    this.predictDeviceState(
      device.family_id,
      device.id,
      nextPlaying ? "playing" : "paused",
    );
    this.render();
    try {
      await api.toggleSonos(device.id, nextPlaying);
    } catch (err) {
      this.clearPendingPrediction(device.family_id, device.id);
      if (err instanceof HttpError && err.status === 409) {
        this.renderActionError(err.detail);
      } else {
        console.warn(`[domesti-bot] toggle ${device.label} failed`, err);
      }
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
    // the global "Turn off / pause / close everything" button.
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

  private async refresh(): Promise<void> {
    // Post-bootstrap refresh. By the time this runs ``init`` has
    // either populated ``this.state`` (success) or rendered the
    // error banner (gave up). A null ``this.state`` here would
    // mean the poll fired before bootstrap finished, which can't
    // happen with the current sequencing (``schedulePoll`` is
    // only called after ``bootstrap``).
    try {
      this.state = await api.fetchState();
      this.connected = true;
      this.applyPendingPredictionsTo(this.state);
      this.render();
    } catch {
      // Network blip mid-session — keep the cached tiles, flip the
      // family frames to red, and let the next tick recover.
      this.connected = false;
      if (this.state) this.render();
    }
  }

  private registerVisibilityPollBoost(): void {
    // When the user returns from another app, catch up immediately instead
    // of waiting for the next interval tick.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      void this.refresh();
    });
  }

  /** Reload tiles from ``/v1/ui/state`` (e.g. after saving settings). */
  async reloadFromServer(): Promise<void> {
    await this.refresh();
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
      header.className = "tile-header tile-header-global";
      const menu = createDesktopMenuButton(this.meta);
      if (menu !== null) {
        header.append(menu);
      }
      header.append(createBrandMark(this.meta));
      const actions = document.createElement("div");
      actions.className = "tile-header-actions";
      const globalBtn = document.createElement("button");
      globalBtn.type = "button";
      globalBtn.className = "btn btn-danger tile-header-global-off";
      globalBtn.textContent = "Turn off / pause / close everything";
      globalBtn.disabled = !this.connected;
      globalBtn.addEventListener("click", () => {
        void this.onBulkOffGlobal();
      });
      actions.append(globalBtn, createThemeToggleButton());
      header.append(actions);
      this.root.append(header);
    } else {
      const emptyHead = document.createElement("header");
      emptyHead.className = "tile-header tile-header-sparse";
      const menu = createDesktopMenuButton(this.meta);
      if (menu !== null) {
        emptyHead.append(menu);
      }
      const sparseActions = document.createElement("div");
      sparseActions.className = "tile-header-actions tile-header-actions-sparse";
      sparseActions.append(createThemeToggleButton());
      emptyHead.append(createBrandMark(this.meta), sparseActions);
      this.root.append(emptyHead);
      const panel = document.createElement("section");
      panel.className = "tile-empty-discovery";
      const h2 = document.createElement("h2");
      h2.textContent = "No devices found on the network";
      const lead = document.createElement("p");
      lead.className = "tile-empty";
      lead.textContent =
        "Discovery finished, but nothing on the LAN responded as a controllable device. That usually means hardware is asleep, on another VLAN, or not configured for this server.";
      const list = document.createElement("ul");
      const tips: readonly string[] = [
        "Confirm plugs, speakers, and garage controllers are powered and on the same routed network as this host.",
        "For Kasa / Tapo, set KASA_USERNAME and KASA_PASSWORD when the LAN handshake requires your cloud account, then run discovery again (CLI: --force-discovery).",
        "For GoTailwind doors, set TAILWIND_TOKEN so the server can reach your controller.",
      ];
      for (const line of tips) {
        const li = document.createElement("li");
        li.textContent = line;
        list.append(li);
      }
      const health = document.createElement("p");
      health.className = "tile-empty";
      const hl = document.createElement("a");
      hl.href = "/health";
      hl.textContent = "/health";
      health.append(
        document.createTextNode("Open "),
        hl,
        document.createTextNode(
          " in this browser to see whether discovery is still running or failed with an error.",
        ),
      );
      panel.append(h2, lead, list, health);
      this.root.append(panel);
      return;
    }

    for (const family of state.families) {
      this.root.append(renderFamily(family, this, this.connected));
    }
  }

  private dismissActionError(): void {
    if (this.actionErrorTimer !== null) {
      window.clearTimeout(this.actionErrorTimer);
      this.actionErrorTimer = null;
    }
    if (this.actionErrorToast !== null) {
      this.actionErrorToast.remove();
      this.actionErrorToast = null;
    }
  }

  private renderActionError(message: string): void {
    // Recoverable per-action error (e.g. a Sonos 409 from an empty
    // queue). Distinct from ``renderError``, which is destructive
    // and reserved for fatal/bootstrap failures. The toast lives in
    // ``document.body`` rather than ``this.root`` so it survives
    // the ``replaceChildren()`` inside ``render()`` — otherwise the
    // background poll would tear it down as soon as it arrived.
    // Calling this again *replaces* any current toast and resets
    // the auto-dismiss timer; we only ever show one at a time so a
    // burst of failed clicks doesn't pile up a wall of alerts.
    this.dismissActionError();

    const toast = document.createElement("div");
    toast.className = "action-toast";
    // ``role=alert`` + ``aria-live=assertive`` make screen readers
    // announce immediately; sighted users get the visual toast.
    toast.setAttribute("role", "alert");
    toast.setAttribute("aria-live", "assertive");

    const text = document.createElement("span");
    text.className = "action-toast-message";
    text.textContent = message;

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "action-toast-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "\u00d7";
    dismiss.addEventListener("click", () => {
      this.dismissActionError();
    });

    toast.append(text, dismiss);
    document.body.append(toast);
    this.actionErrorToast = toast;
    this.actionErrorTimer = window.setTimeout(() => {
      this.dismissActionError();
    }, DomestiBotController.ACTION_ERROR_TOAST_MS);
  }

  private renderError(prefix: string, err: unknown): void {
    const detail = err instanceof HttpError
      ? `${err.status}: ${err.bodyText.slice(0, 200)}`
      : err instanceof Error
        ? err.message
        : String(err);
    this.root.replaceChildren();
    const errHead = document.createElement("header");
    errHead.className = "tile-header tile-header-sparse";
    errHead.append(createBrandMark(this.meta), createThemeToggleButton());
    this.root.append(errHead);
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

  private renderLoading(message: string): void {
    // Initial paint while the FastAPI lifespan is still running
    // device discovery in the background. Pairs a CSS-only spinner
    // (see ``.tile-spinner`` in ``index.html``) with a short text
    // so screen-reader users get a verbal cue too.
    this.root.replaceChildren();
    const loadHead = document.createElement("header");
    loadHead.className = "tile-header tile-header-sparse";
    loadHead.append(createBrandMark(this.meta), createThemeToggleButton());
    this.root.append(loadHead);
    const row = document.createElement("div");
    row.className = "tile-loading-row";
    row.setAttribute("role", "status");
    row.setAttribute("aria-live", "polite");
    const spinner = document.createElement("span");
    spinner.className = "tile-spinner";
    spinner.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.className = "tile-loading";
    label.textContent = message;
    row.append(spinner, label);
    this.root.append(row);
  }

  private static sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      window.setTimeout(resolve, ms);
    });
  }

  toggleKasaTile(device: UIDeviceOut): void {
    void this.onToggleKasa(device);
  }

  toggleSonosTile(device: UIDeviceOut): void {
    void this.onToggleSonos(device);
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

// Inline-SVG path commands for each family's header icon. Stroke-only,
// 24×24 viewBox, so a single ``color: var(--family-color)`` on
// ``.family-icon`` paints the whole silhouette via ``currentColor``.
// Adding a new family is a one-line addition here plus the existing
// ``color`` entry in ``app.api.ui_state._FAMILIES``. Shapes are
// deliberately spartan (lightbulb / speaker cabinet / pitched-roof
// house with panel lines) so they read clearly at 1.4em.
const FAMILY_ICON_PATHS: Record<string, readonly string[]> = {
  kasa: [
    "M9 18h6",
    "M10 22h4",
    "M12 2a7 7 0 0 0-4 12.7c.6.5 1 1.3 1 2.1V18h6v-1.2c0-.8.4-1.6 1-2.1A7 7 0 0 0 12 2z",
  ],
  sonos: [
    "M7 2h10a2 2 0 0 1 2 2v16a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2z",
    "M12 18a4 4 0 1 0 0-8 4 4 0 0 0 0 8z",
    "M12 6.5a1 1 0 1 0 0 2 1 1 0 0 0 0-2z",
  ],
  tailwind: [
    "M3 11l9-7 9 7",
    "M5 10v11h14V10",
    "M5 14h14",
    "M5 17.5h14",
  ],
};

const SVG_NS = "http://www.w3.org/2000/svg";

function applyStoredColorTheme(): void {
  const raw = localStorage.getItem(THEME_STORAGE_KEY);
  const t = raw === "light" || raw === "dark" ? raw : null;
  if (t === null) {
    document.documentElement.removeAttribute("data-theme");
  } else {
    document.documentElement.setAttribute("data-theme", t);
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta !== null) {
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const dark = t === "dark" || (t === null && systemDark);
    meta.setAttribute("content", dark ? "#15171a" : "#0a0a0a");
  }
  if (themeToggleSingleton !== null) {
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const darkNow = t === "dark" || (t === null && systemDark);
    themeToggleSingleton.innerHTML = darkNow ? THEME_GLYPH_SUN_SVG : THEME_GLYPH_MOON_SVG;
    const title = darkNow
      ? "Switch to light appearance"
      : "Switch to dark appearance";
    themeToggleSingleton.title = title;
    themeToggleSingleton.setAttribute("aria-label", title);
  }
}

function bulkOffStateForKind(kind: UIDeviceOut["kind"]): UIDeviceState {
  // What the backend's bulk-off endpoints actually drive each device
  // kind to. Used by the controller's optimistic-prediction helpers
  // so the UI can show the post-action state before the round-trip
  // (and the post-action poll) lands.
  switch (kind) {
    case "switch":
      return "off";
    case "speaker":
      return "paused";
    case "door":
      return "closed";
  }
}

/** Robot-with-apron mascot: click opens GitHub; hover shows product, tagline, copyright, license, build, and repo link. */
function createBrandMark(meta: MetaOut | null): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "brand-mark";
  wrap.setAttribute("role", "group");
  wrap.setAttribute(
    "aria-label",
    meta
      ? `domesti-bot, version ${meta.version}, commit ${meta.commit}, GitHub repository`
      : "domesti-bot, GitHub repository, build information loading",
  );

  const iconLink = document.createElement("a");
  iconLink.className = "brand-mark-icon-link";
  iconLink.href = DOMESTI_BOT_REPO_HREF;
  iconLink.target = "_blank";
  iconLink.rel = "noopener noreferrer";
  iconLink.setAttribute(
    "aria-label",
    "domesti-bot — open github.com/the-hcma/domesti-bot in a new tab",
  );

  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "brand-mark-svg");
  svg.setAttribute("width", "30");
  svg.setAttribute("height", "30");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");

  const body = document.createElementNS(SVG_NS, "rect");
  body.setAttribute("class", "brand-mark-bm-body");
  body.setAttribute("x", "5.5");
  body.setAttribute("y", "12.8");
  body.setAttribute("width", "13");
  body.setAttribute("height", "10");
  body.setAttribute("rx", "2.5");
  body.setAttribute("stroke-width", "1.2");
  body.setAttribute("stroke-linecap", "round");
  body.setAttribute("stroke-linejoin", "round");

  const apron = document.createElementNS(SVG_NS, "path");
  apron.setAttribute("class", "brand-mark-bm-apron");
  apron.setAttribute("d", "M6 13.2 L18 13.2 L17.2 20.8 Q12 22.4 6.8 20.8 Z");
  apron.setAttribute("stroke-width", "1.05");
  apron.setAttribute("stroke-linecap", "round");
  apron.setAttribute("stroke-linejoin", "round");

  const waist = document.createElementNS(SVG_NS, "path");
  waist.setAttribute("class", "brand-mark-bm-waist");
  waist.setAttribute("d", "M7.5 18.5h9");
  waist.setAttribute("stroke-width", "1.05");
  waist.setAttribute("stroke-linecap", "round");

  const neck = document.createElementNS(SVG_NS, "rect");
  neck.setAttribute("class", "brand-mark-bm-neck");
  neck.setAttribute("x", "9.4");
  neck.setAttribute("y", "11.4");
  neck.setAttribute("width", "5.2");
  neck.setAttribute("height", "2.4");
  neck.setAttribute("rx", "0.9");
  neck.setAttribute("stroke-width", "0.9");
  neck.setAttribute("stroke-linecap", "round");
  neck.setAttribute("stroke-linejoin", "round");

  const head = document.createElementNS(SVG_NS, "rect");
  head.setAttribute("class", "brand-mark-bm-head");
  head.setAttribute("fill", "none");
  head.setAttribute("x", "4.8");
  head.setAttribute("y", "3.35");
  head.setAttribute("width", "14.4");
  head.setAttribute("height", "9.05");
  head.setAttribute("rx", "3.2");
  head.setAttribute("stroke-width", "1.2");
  head.setAttribute("stroke-linecap", "round");
  head.setAttribute("stroke-linejoin", "round");

  const antennaRod = document.createElementNS(SVG_NS, "line");
  antennaRod.setAttribute("class", "brand-mark-bm-antenna-rod");
  antennaRod.setAttribute("x1", "12");
  antennaRod.setAttribute("y1", "3.35");
  antennaRod.setAttribute("x2", "12");
  antennaRod.setAttribute("y2", "0.65");
  antennaRod.setAttribute("stroke-width", "1.15");
  antennaRod.setAttribute("stroke-linecap", "round");

  const antennaBall = document.createElementNS(SVG_NS, "circle");
  antennaBall.setAttribute("class", "brand-mark-bm-antenna-ball");
  antennaBall.setAttribute("cx", "12");
  antennaBall.setAttribute("cy", "0.55");
  antennaBall.setAttribute("r", "0.85");
  antennaBall.setAttribute("stroke-width", "0.55");

  const eyeL = document.createElementNS(SVG_NS, "circle");
  eyeL.setAttribute("class", "brand-mark-bm-eye");
  eyeL.setAttribute("cx", "9.15");
  eyeL.setAttribute("cy", "7.05");
  eyeL.setAttribute("r", "1.22");

  const eyeR = document.createElementNS(SVG_NS, "circle");
  eyeR.setAttribute("class", "brand-mark-bm-eye");
  eyeR.setAttribute("cx", "14.85");
  eyeR.setAttribute("cy", "7.05");
  eyeR.setAttribute("r", "1.22");

  const mouth = document.createElementNS(SVG_NS, "path");
  mouth.setAttribute("class", "brand-mark-bm-mouth");
  mouth.setAttribute("d", "M8.15 9.35 Q12 11.05 15.85 9.35");

  svg.append(
    body,
    apron,
    waist,
    neck,
    head,
    antennaRod,
    antennaBall,
    eyeL,
    eyeR,
    mouth,
  );
  iconLink.append(svg);

  const tip = document.createElement("span");
  tip.className = "brand-mark-tooltip";
  tip.setAttribute("role", "tooltip");

  const product = document.createElement("div");
  product.className = "brand-mark-tooltip-product";
  product.textContent = "domesti-bot";

  const tagline = document.createElement("div");
  tagline.className = "brand-mark-tooltip-tagline";
  tagline.textContent =
    "Home LAN dashboard for Kasa, Sonos, GoTailwind, and more.";

  const rights = document.createElement("div");
  rights.className = "brand-mark-tooltip-copy";
  rights.textContent = "\u00a9 2026 Henrique Andrade";

  const license = document.createElement("div");
  license.className = "brand-mark-tooltip-license";
  license.textContent = "Open-source software under the MIT License.";

  tip.append(product, tagline, rights, license);

  if (meta) {
    const v = document.createElement("div");
    v.className = "brand-mark-tooltip-build";
    v.textContent = `Version ${meta.version}`;
    const c = document.createElement("div");
    c.className = "brand-mark-tooltip-build";
    c.textContent = `Commit ${meta.commit}`;
    tip.append(v, c);
  } else {
    const pending = document.createElement("div");
    pending.className = "brand-mark-tooltip-build";
    pending.textContent = "Loading build info\u2026";
    tip.append(pending);
  }

  const repoLink = document.createElement("a");
  repoLink.className = "brand-mark-tooltip-repo";
  repoLink.href = DOMESTI_BOT_REPO_HREF;
  repoLink.target = "_blank";
  repoLink.rel = "noopener noreferrer";
  repoLink.textContent = "github.com/the-hcma/domesti-bot";
  tip.append(repoLink);

  wrap.append(iconLink, tip);

  let tipPinned = false;
  const syncPos = (): void => {
    syncBrandMarkTooltipPosition(iconLink, tip);
  };
  const openTooltip = (): void => {
    if (tipPinned) return;
    tipPinned = true;
    tip.classList.add("is-open");
    syncBrandMarkTooltipPosition(iconLink, tip);
    window.addEventListener("resize", syncPos);
    window.addEventListener("scroll", syncPos, true);
  };
  const closeTooltip = (): void => {
    if (!tipPinned) return;
    tipPinned = false;
    tip.classList.remove("is-open");
    tip.style.removeProperty("left");
    tip.style.removeProperty("top");
    tip.style.removeProperty("position");
    tip.style.removeProperty("display");
    window.removeEventListener("resize", syncPos);
    window.removeEventListener("scroll", syncPos, true);
  };
  wrap.addEventListener("pointerenter", openTooltip);
  wrap.addEventListener("pointerleave", (ev) => {
    const rel = ev.relatedTarget as Node | null;
    if (rel && wrap.contains(rel)) {
      return;
    }
    closeTooltip();
  });
  wrap.addEventListener("focusin", () => {
    openTooltip();
  });
  wrap.addEventListener("focusout", (ev) => {
    if (!wrap.contains(ev.relatedTarget as Node | null)) {
      closeTooltip();
    }
  });

  return wrap;
}

function createFamilyIcon(familyId: string): SVGElement | null {
  // Returns a configured ``<svg>`` element for the family header,
  // or ``null`` if the family doesn't have a registered icon (in
  // which case the heading just shows its text label — the per-tile
  // ``border-left`` already carries the family colour). The SVG is
  // built in the SVG namespace (``createElementNS``) because plain
  // ``createElement`` would produce HTMLUnknownElement nodes that
  // browsers won't render as SVG.
  const paths = FAMILY_ICON_PATHS[familyId];
  if (!paths) return null;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "family-icon");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  // Purely decorative — the adjacent ``<h3>`` text already labels
  // the family for screen readers.
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}

let openAppMenuCloser: (() => void) | null = null;

function appendTailwindTokenIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  const link = document.createElement("a");
  link.href = TAILWIND_WEB_DASHBOARD_HREF;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "Tailwind web dashboard";
  intro.append(
    document.createTextNode("Copy the six-digit token from the "),
    link,
    document.createTextNode(
      ". It is stored encrypted in the discovery database on this server.",
    ),
  );
  parent.append(intro);
}

function closeAppMenu(): void {
  if (openAppMenuCloser !== null) {
    openAppMenuCloser();
    openAppMenuCloser = null;
  }
}

function createDesktopMenuButton(meta: MetaOut | null): HTMLDivElement | null {
  if (isMobileFormFactor()) {
    return null;
  }
  const wrap = document.createElement("div");
  wrap.className = "app-menu";
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "btn btn-menu";
  trigger.setAttribute("aria-label", "Open menu");
  trigger.setAttribute("aria-haspopup", "menu");
  trigger.setAttribute("aria-expanded", "false");
  trigger.innerHTML =
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16"/></svg>';
  const panel = document.createElement("div");
  panel.className = "app-menu-panel";
  panel.setAttribute("role", "menu");
  panel.hidden = true;
  const settingsItem = document.createElement("button");
  settingsItem.type = "button";
  settingsItem.className = "app-menu-item";
  settingsItem.setAttribute("role", "menuitem");
  settingsItem.textContent = "Settings";
  settingsItem.addEventListener("click", () => {
    closeAppMenu();
    void openTailwindSettingsDialog();
  });
  const aboutItem = document.createElement("button");
  aboutItem.type = "button";
  aboutItem.className = "app-menu-item";
  aboutItem.setAttribute("role", "menuitem");
  aboutItem.textContent = "About";
  aboutItem.addEventListener("click", () => {
    closeAppMenu();
    openAboutDialog(meta);
  });
  panel.append(settingsItem, aboutItem);
  wrap.append(trigger, panel);

  const onDocumentClick = (ev: MouseEvent): void => {
    if (!wrap.contains(ev.target as Node)) {
      closeAppMenu();
    }
  };

  trigger.addEventListener("click", () => {
    if (panel.hidden) {
      panel.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      openAppMenuCloser = () => {
        panel.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        document.removeEventListener("click", onDocumentClick, true);
      };
      window.setTimeout(() => {
        document.addEventListener("click", onDocumentClick, true);
      }, 0);
    } else {
      closeAppMenu();
    }
  });

  return wrap;
}

function openAboutDialog(meta: MetaOut | null): void {
  const dialog = document.createElement("dialog");
  dialog.className = "settings-dialog about-dialog";
  const title = document.createElement("h2");
  title.textContent = "About domesti-bot";
  const body = document.createElement("p");
  body.className = "settings-dialog-lead";
  body.textContent =
    "Self-hosted LAN dashboard for TP-Link Kasa, Sonos, and GoTailwind garage doors.";
  const version = document.createElement("p");
  version.className = "settings-dialog-status";
  version.textContent = meta
    ? `Version ${meta.version} · commit ${meta.commit}`
    : "Loading build info…";
  const repo = document.createElement("a");
  repo.className = "about-dialog-repo";
  repo.href = DOMESTI_BOT_REPO_HREF;
  repo.target = "_blank";
  repo.rel = "noopener noreferrer";
  repo.textContent = "github.com/the-hcma/domesti-bot";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn";
  closeBtn.textContent = "Close";
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  actions.append(closeBtn);
  dialog.append(title, body, version, repo, actions);
  document.body.append(dialog);
  dialog.addEventListener("close", () => {
    dialog.remove();
  });
  dialog.addEventListener("click", (ev) => {
    if (ev.target === dialog) {
      dialog.close();
    }
  });
  dialog.showModal();
}

async function openTailwindSettingsDialog(): Promise<void> {
  const dialog = document.createElement("dialog");
  dialog.className = "settings-dialog";
  const form = document.createElement("form");
  let reloadDevicesAfterClose = false;
  const title = document.createElement("h2");
  title.textContent = "GoTailwind token";
  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;
  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = "Token";
  const tokenRow = document.createElement("div");
  tokenRow.className = "settings-dialog-token-row";
  const input = document.createElement("input");
  input.type = "password";
  input.name = "token";
  input.autocomplete = "off";
  input.inputMode = "numeric";
  input.maxLength = 64;
  input.required = true;
  const revealBtn = document.createElement("button");
  revealBtn.type = "button";
  revealBtn.className = "btn settings-dialog-reveal";
  revealBtn.setAttribute("aria-label", "Show token");
  revealBtn.setAttribute("aria-pressed", "false");
  let storedToken: string | null = null;
  let tokenRevealed = false;
  const setTokenRevealed = (revealed: boolean): void => {
    tokenRevealed = revealed;
    if (revealed && !input.value && storedToken) {
      input.value = storedToken;
    }
    input.type = revealed ? "text" : "password";
    revealBtn.innerHTML = revealed ? TOKEN_REVEAL_EYE_OFF_SVG : TOKEN_REVEAL_EYE_SVG;
    revealBtn.setAttribute("aria-label", revealed ? "Hide token" : "Show token");
    revealBtn.setAttribute("aria-pressed", revealed ? "true" : "false");
  };
  setTokenRevealed(false);
  revealBtn.addEventListener("click", () => {
    setTokenRevealed(!tokenRevealed);
  });
  tokenRow.append(input, revealBtn);
  label.append(labelText, tokenRow);
  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn";
  clearBtn.textContent = "Clear stored token";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "btn";
  closeBtn.textContent = "Close";
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  actions.append(saveBtn, clearBtn, closeBtn);
  form.append(title);
  appendTailwindTokenIntro(form);
  form.append(status, label, actions);
  dialog.append(form);
  document.body.append(dialog);

  const applyTokenFieldsFromSettings = (s: TailwindTokenSettingsOut): void => {
    storedToken = s.stored_token;
    if (storedToken) {
      input.value = storedToken;
      input.required = false;
      if (!tokenRevealed) {
        input.type = "password";
      }
    } else {
      input.required = true;
    }
    input.placeholder = storedToken ? "" : "Six-digit token";
  };

  const showStatusMessage = (message: string): void => {
    status.textContent = message;
    status.hidden = false;
  };

  const hideStatus = (): void => {
    status.textContent = "";
    status.hidden = true;
  };

  const updateStatusHint = (s: TailwindTokenSettingsOut): void => {
    if (!s.secrets_key_configured) {
      showStatusMessage(
        "Add domesti_secrets_key to domesti-secrets.json at the repo root (see domesti-secrets.json.example) or set DOMESTI_SECRETS_KEY before saving to the database.",
      );
      return;
    }
    if (s.source === "env" || s.source === "cli") {
      showStatusMessage(
        "TAILWIND_TOKEN (or --tailwind-token) overrides the database until you remove it.",
      );
      return;
    }
    hideStatus();
  };

  const refreshStatus = async (): Promise<void> => {
    try {
      const s = await api.fetchTailwindTokenSettings();
      applyTokenFieldsFromSettings(s);
      updateStatusHint(s);
    } catch (err) {
      showStatusMessage(
        err instanceof HttpError ? err.detail : "Could not load token status.",
      );
    }
  };

  const saveToken = (): void => {
    void (async () => {
      const token = input.value.trim();
      if (!token) {
        showStatusMessage("Enter a token before saving.");
        return;
      }
      saveBtn.disabled = true;
      try {
        const out = await api.putTailwindToken(token);
        showSettingsToast("Token saved.");
        reloadDevicesAfterClose = !out.restart_required;
        setTokenRevealed(false);
        const s = await api.fetchTailwindTokenSettings();
        applyTokenFieldsFromSettings(s);
        if (out.restart_required) {
          showStatusMessage(
            "Token saved. Restart domesti-bot (or remove TAILWIND_TOKEN) so garage doors use it.",
          );
        } else {
          updateStatusHint(s);
        }
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Save failed.",
        );
      } finally {
        saveBtn.disabled = false;
      }
    })();
  };
  saveBtn.addEventListener("click", saveToken);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveToken();
  });

  clearBtn.addEventListener("click", () => {
    void (async () => {
      try {
        await api.clearTailwindToken();
        storedToken = null;
        input.value = "";
        input.required = true;
        setTokenRevealed(false);
        showSettingsToast("Stored token cleared.");
        reloadDevicesAfterClose = true;
        await refreshStatus();
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Clear failed.",
        );
      }
    })();
  });

  dialog.addEventListener("close", () => {
    dialog.remove();
    if (reloadDevicesAfterClose) {
      reloadDevicesAfterClose = false;
      void domestiUiController?.reloadFromServer();
    }
  });
  dialog.addEventListener("click", (ev) => {
    if (ev.target === dialog) {
      dialog.close();
    }
  });

  await refreshStatus();
  dialog.showModal();
}

function showSettingsToast(message: string): void {
  if (settingsToastTimer !== null) {
    window.clearTimeout(settingsToastTimer);
    settingsToastTimer = null;
  }
  if (settingsToast !== null) {
    settingsToast.remove();
    settingsToast = null;
  }
  const toast = document.createElement("div");
  toast.className = "action-toast action-toast-success";
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  const text = document.createElement("p");
  text.className = "action-toast-message";
  text.textContent = message;
  toast.append(text);
  document.body.append(toast);
  settingsToast = toast;
  settingsToastTimer = window.setTimeout(() => {
    toast.remove();
    if (settingsToast === toast) {
      settingsToast = null;
    }
    settingsToastTimer = null;
  }, SETTINGS_TOAST_MS);
}

function createThemeToggleButton(): HTMLButtonElement {
  if (themeToggleSingleton !== null) {
    return themeToggleSingleton;
  }
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn btn-theme-toggle";
  btn.addEventListener("click", () => {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    const explicit = raw === "light" || raw === "dark" ? raw : null;
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const darkNow = explicit === "dark" || (explicit === null && systemDark);
    localStorage.setItem(THEME_STORAGE_KEY, darkNow ? "light" : "dark");
    applyStoredColorTheme();
  });
  const mql = window.matchMedia("(prefers-color-scheme: dark)");
  mql.addEventListener("change", () => {
    const raw = localStorage.getItem(THEME_STORAGE_KEY);
    if (raw !== "light" && raw !== "dark") {
      applyStoredColorTheme();
    }
  });
  themeToggleSingleton = btn;
  applyStoredColorTheme();
  return btn;
}

function initPwaInstallBanner(): void {
  if (window.matchMedia("(display-mode: standalone)").matches) {
    return;
  }
  const nav = window.navigator as Navigator & { standalone?: boolean };
  if (nav.standalone === true) {
    return;
  }
  if (localStorage.getItem(PWA_INSTALL_DISMISS_PERMANENT_KEY) === "1") {
    return;
  }
  if (
    sessionStorage.getItem(PWA_INSTALL_DISMISS_SESSION_KEY) === "1" ||
    sessionStorage.getItem("domesti-pwa-install-dismissed") === "1"
  ) {
    return;
  }

  if (!isMobileFormFactor()) {
    return;
  }

  const mainEl = document.querySelector("main");
  if (mainEl === null) {
    return;
  }

  const banner = document.createElement("aside");
  banner.className = "pwa-install-banner";
  banner.setAttribute("aria-label", "Install web app");

  const title = document.createElement("p");
  title.className = "pwa-install-banner-title";
  title.textContent = "Install domesti-bot";

  const copy = document.createElement("p");
  copy.className = "pwa-install-banner-copy";
  copy.textContent =
    "Add this dashboard to your home screen for one-tap access. Use Install below when your browser enables it; otherwise open the browser menu and choose Add to Home screen or Install app.";

  const persistRow = document.createElement("div");
  persistRow.className = "pwa-install-persist-row";
  const persistCb = document.createElement("input");
  persistCb.type = "checkbox";
  persistCb.id = "pwa-install-do-not-ask";
  persistCb.className = "pwa-install-persist-cb";
  const persistLabel = document.createElement("label");
  persistLabel.className = "pwa-install-persist-label";
  persistLabel.htmlFor = "pwa-install-do-not-ask";
  persistLabel.textContent = "Do not ask again";

  const actions = document.createElement("div");
  actions.className = "pwa-install-actions";

  const installBtn = document.createElement("button");
  installBtn.type = "button";
  installBtn.className = "btn pwa-install-btn";
  installBtn.textContent = "Install";
  installBtn.hidden = true;

  const dismissBtn = document.createElement("button");
  dismissBtn.type = "button";
  dismissBtn.className = "btn pwa-dismiss-btn";
  dismissBtn.textContent = "Dismiss";

  let deferred: PwaBeforeInstallPromptEvent | null = null;

  const onBeforeInstall = (ev: Event): void => {
    ev.preventDefault();
    deferred = ev as PwaBeforeInstallPromptEvent;
    installBtn.hidden = false;
  };

  const dismiss = (): void => {
    if (persistCb.checked) {
      localStorage.setItem(PWA_INSTALL_DISMISS_PERMANENT_KEY, "1");
    } else {
      sessionStorage.setItem(PWA_INSTALL_DISMISS_SESSION_KEY, "1");
    }
    banner.remove();
    window.removeEventListener("beforeinstallprompt", onBeforeInstall);
  };

  window.addEventListener("beforeinstallprompt", onBeforeInstall);

  installBtn.addEventListener("click", () => {
    void (async () => {
      if (deferred === null) {
        return;
      }
      await deferred.prompt();
      void deferred.userChoice;
      dismiss();
    })();
  });

  dismissBtn.addEventListener("click", dismiss);

  persistRow.append(persistCb, persistLabel);
  actions.append(installBtn, dismissBtn);
  banner.append(title, copy, persistRow, actions);
  mainEl.insertBefore(banner, mainEl.firstChild);
}

function isMobileFormFactor(): boolean {
  const uaData = (
    navigator as Navigator & { userAgentData?: { mobile?: boolean } }
  ).userAgentData;
  if (uaData?.mobile === true) {
    return true;
  }
  if (uaData?.mobile === false) {
    return false;
  }
  // No Client Hints (common on WebKit) — infer compact, touch-first UI without
  // matching particular browser names.
  return (
    window.matchMedia("(any-pointer: coarse)").matches ||
    window.matchMedia("(pointer: coarse)").matches ||
    window.matchMedia("(max-width: 768px)").matches
  );
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
  // their head. ``unknown`` defaults to the safer destructive action
  // for each kind: doors close (transient OPENING/CLOSING → next
  // click closes), speakers pause (stopped queue → next click
  // pauses, a SoCo no-op).
  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "tile-toggle";
  let isActive: boolean;
  let actionLabel: string;
  let excludeText: string;
  if (device.kind === "switch") {
    isActive = device.state === "on";
    actionLabel = isActive ? "Turn it off" : "Turn it on";
    excludeText = "Exclude from all-off";
    toggle.addEventListener("click", () => {
      controller.toggleKasaTile(device);
    });
  } else if (device.kind === "speaker") {
    isActive = device.state === "playing";
    actionLabel = isActive ? "Pause it" : "Resume it";
    excludeText = "Exclude from pause-all";
    toggle.addEventListener("click", () => {
      controller.toggleSonosTile(device);
    });
  } else {
    isActive = device.state === "open";
    actionLabel = isActive ? "Close it" : "Open it";
    excludeText = "Exclude from close-all";
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
  const icon = createFamilyIcon(family.id);
  if (icon !== null) {
    heading.append(icon);
  }
  // ``append(textNode)`` (rather than ``textContent =``) so any
  // already-appended icon survives.
  heading.append(document.createTextNode(family.label));
  header.append(heading);

  if (
    family.id === "kasa" ||
    family.id === "sonos" ||
    family.id === "tailwind"
  ) {
    const bulkBtn = document.createElement("button");
    bulkBtn.type = "button";
    // Per-family bulk-off is always destructive ("Turn off all" /
    // "Pause all" / "Close all"), so use the same red ``btn-danger``
    // styling as the global "Turn off / pause / close everything" button —
    // colors stay consistent with the green/red rule that drives the
    // state badges and the per-tile toggles.
    bulkBtn.className = "btn btn-danger";
    bulkBtn.textContent =
      family.id === "kasa"
        ? "Turn off all"
        : family.id === "sonos"
          ? "Pause all"
          : "Close all";
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

function registerServiceWorker(): void {
  if (!("serviceWorker" in navigator)) {
    return;
  }
  const { protocol, hostname } = window.location;
  const allowed =
    protocol === "https:" ||
    hostname === "localhost" ||
    hostname === "127.0.0.1";
  if (!allowed) {
    return;
  }
  window.addEventListener(
    "load",
    () => {
      void navigator.serviceWorker.register("/sw.js", { scope: "/" });
    },
    { once: true },
  );
}

function removeJsBootHint(): void {
  document.getElementById("app-js-boot-hint")?.remove();
}

function start(): void {
  removeJsBootHint();
  applyStoredColorTheme();
  initPwaInstallBanner();
  registerServiceWorker();
  const root = document.getElementById(APP_ROOT_ID);
  if (!root) {
    console.warn(`[domesti-bot] expected #${APP_ROOT_ID} in landing page`);
    return;
  }
  const controller = new DomestiBotController(root);
  domestiUiController = controller;
  void controller.init();
}

function syncBrandMarkTooltipPosition(anchor: HTMLElement, tip: HTMLElement): void {
  if (!tip.classList.contains("is-open")) {
    return;
  }
  const margin = 10;
  const ar = anchor.getBoundingClientRect();
  tip.style.position = "fixed";
  tip.style.display = "block";
  const tw = tip.offsetWidth;
  const th = tip.offsetHeight;
  let top = ar.bottom + margin;
  if (top + th > window.innerHeight - margin) {
    top = ar.top - th - margin;
  }
  let left = ar.left;
  if (left + tw > window.innerWidth - margin) {
    left = window.innerWidth - tw - margin;
  }
  if (left < margin) {
    left = margin;
  }
  if (top < margin) {
    top = margin;
  }
  tip.style.left = `${Math.round(left)}px`;
  tip.style.top = `${Math.round(top)}px`;
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
