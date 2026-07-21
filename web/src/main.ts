// Browser entrypoint for the domesti-bot landing page.
//
// Hydrates the empty ``<div id="app">`` in ``app/api/static/index.html``
// with the tile UI: one family-section per device family, one tile per
// device, plus per-family and global bulk actions.

import { api, HttpError, isBackendTransportFailure } from "./api.js";
import {
  createEp1HeaderStatusStrip,
  MOCK_EP1_HEADER_STATUS,
} from "./ep1-header-status.js";
import { openSettingsHubDialog } from "./settings-hub-dialog.js";
import { openAutomationsHubDialog, parseAutomationsDeepLink } from "./rules-dialog.js";
import {
  UIDeviceKind,
  UIDeviceState,
  type MetaOut,
  type UIBulkActionOut,
  type UIDeviceOut,
  type UIFamilyOut,
  type UIOccupancyReadingsOut,
  type UIOperatorAlertOut,
  type UIStateOut,
} from "./types.js";

const APP_ROOT_ID = "app";

/** Viewport / device query for compact tiles + compact header chrome.
 *
 * Portrait phones match ``max-width: 768px``. Landscape phones often have a CSS
 * width above 768 (e.g. ~844–932) while height is phone-sized — without the
 * second clause they incorrectly get comfortable layout (one-line EP1 readings
 * and the long bulk-off sentence instead of icons).
 */
const COMPACT_LAYOUT_MQ =
  "(max-width: 768px), (max-height: 560px) and (hover: none) and (pointer: coarse)";

/** Compact tile labels: binary-search bounds for fitted label size (px). */
const COMPACT_LABEL_FONT_MIN_PX = 11;
const COMPACT_LABEL_FONT_MAX_PX = 30;
/** Global bulk-off button on compact layout (separate from tile labels). */
const COMPACT_BULK_FONT_MIN_PX = 11;
const COMPACT_BULK_FONT_MAX_PX = 18;

const DEVICE_PROPERTIES_EXCLUDE_HELP =
  "Skip this device when using the global Turn off / pause / close everything control. Family bulk buttons and this tile's own toggle still work.";
const DEVICE_PROPERTIES_EXCLUDE_LABEL =
  "Exclude from Turn off / Pause / Close everything";
/** Accessible label (and comfortable layout text) for the global bulk-off control. */
const GLOBAL_BULK_OFF_LABEL = "Turn off / pause / close everything";
const DEVICE_PROPERTIES_HIDE_HELP =
  "Hide this tile on the compact (phone / tablet) layout. It remains visible and controllable on the desktop web UI.";
const DEVICE_PROPERTIES_HIDE_LABEL = "Hide on phone / tablet";
const DEVICE_PROPERTIES_LONG_PRESS_MS = 500;
const DEVICE_PROPERTIES_MENU_HINT = "Right-click to edit device properties";

const PWA_INSTALL_DISMISS_PERMANENT_KEY = "domesti-pwa-install-dismiss-permanent";
const PWA_INSTALL_DISMISS_SESSION_KEY = "domesti-pwa-install-dismiss-session";

const THEME_STORAGE_KEY = "domesti-color-theme";

/** Moon icon — shown when UI is light (control switches to dark). */
const THEME_GLYPH_MOON_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

/** Sun icon — shown when UI is dark (control switches to light). */
const THEME_GLYPH_SUN_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>';

let themeToggleSingleton: HTMLButtonElement | null = null;

/** Chromium-only: deferred until the user taps our Install control. */
interface PwaBeforeInstallPromptEvent extends Event {
  readonly userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
  prompt: () => Promise<void>;
}

/** Public source repository (About dialog link target). */
const DOMESTI_BOT_REPO_HREF = "https://github.com/the-hcma/domesti-bot";
const ABOUT_TAGLINE =
  "Self-hosted home LAN dashboard for TP-Link Kasa, Sonos, and GoTailwind garage doors.";
const ABOUT_COPYRIGHT = "\u00a9 2026 Henrique Andrade";
const ABOUT_LICENSE = "Open-source software under the MIT License.";
const ABOUT_REPO_LABEL = "github.com/the-hcma/domesti-bot";

let domestiUiController: DomestiBotController | null = null;

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

  // Cap queued device/bulk actions while the client re-establishes transport.
  private static readonly ACTION_BUFFER_MAX = 32;

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

  // While the client re-establishes transport after foreground return, keep
  // frames green and buffer actions for this window before probing /health.
  // Anchored to assessing start (not last poll success) so a phone returning
  // from background gets a fresh grace even when the last poll was minutes ago.
  private static readonly CONNECTION_RECONNECT_GRACE_MS = 3000;

  // How long a recoverable action error (e.g. Sonos 409 "queue is
  // empty") stays visible before auto-dismissing. Long enough to
  // read a one-sentence hint, short enough not to overstay if the
  // user has already moved on. A subsequent action error replaces
  // the current toast immediately and restarts the timer; clicking
  // the ``×`` button dismisses on demand.
  private static readonly ACTION_ERROR_TOAST_MS = 10000;

  private readonly root: HTMLElement;
  private state: UIStateOut | null = null;
  private bufferedActions: Array<() => Promise<void>> = [];
  private bufferFlushInFlight = false;
  private connected = false;
  // True while the client re-establishes transport after a prior successful
  // poll — UI stays connected and user actions are queued for flush.
  private connectionAssessing = false;
  private connectionAssessingStartedAt: number | null = null;
  private devicesReady = false;
  private lastSuccessfulPollAt: number | null = null;
  // Consecutive transport failures (no HTTP response) on /v1/ui/state before
  // we probe /health and possibly mark the UI offline.
  private pollTransportFailureStreak = 0;
  private static readonly POLL_TRANSPORT_FAILURES_BEFORE_VERIFY = 1;
  private refreshInFlight = false;
  private pollTimer: number | null = null;
  // The recoverable-action toast lives outside ``#app`` so it
  // survives the ``replaceChildren()`` calls inside ``render()``.
  // ``null`` means no toast is currently mounted; the timer is
  // cleared whenever we dismiss or replace the toast so we don't
  // accidentally remove a *newer* toast when an older one's timer
  // fires.
  private actionToast: HTMLDivElement | null = null;
  private actionToastTimer: number | null = null;
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
    this.registerCompactLayoutListener();
    this.registerCompactTypographyResize();
    this.registerVisibilityPollBoost();
  }

  private async bootstrap(): Promise<void> {
    const deadline =
      performance.now() + DomestiBotController.BOOTSTRAP_DEADLINE_MS;
    while (true) {
      try {
        this.state = await api.fetchState();
        this.markBackendReadyFromState();
        this.applyPendingPredictionsTo(this.state);
        warmCompactTileIcons(this.state);
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
          await DomestiBotController.sleep(
            DomestiBotController.BOOTSTRAP_RETRY_MS,
          );
          continue;
        }
        this.renderError("Failed to load device state", err);
        return;
      }
    }
  }

  private beginConnectionAssessing(): void {
    if (!this.state || this.lastSuccessfulPollAt === null) {
      return;
    }
    if (!this.connectionAssessing) {
      this.connectionAssessingStartedAt = performance.now();
    }
    this.connectionAssessing = true;
    // After a real outage the UI may still be red even though the LAN link
    // is back — show cached tiles as reachable while we re-poll /v1/ui/state.
    this.markBackendReachableFromCachedSession();
  }

  private bufferAction(execute: () => Promise<void>): void {
    if (
      this.bufferedActions.length
      >= DomestiBotController.ACTION_BUFFER_MAX
    ) {
      console.warn("[domesti-bot] action buffer full, dropping oldest");
      this.bufferedActions.shift();
    }
    this.bufferedActions.push(execute);
  }

  private clearBufferedActions(reason: string): void {
    const dropped = this.bufferedActions.length;
    if (dropped === 0) {
      return;
    }
    this.bufferedActions = [];
    console.warn(
      `[domesti-bot] dropped ${dropped} buffered action(s) — ${reason}`,
    );
  }

  private connectionReconnectGraceActive(): boolean {
    const startedAt = this.connectionAssessingStartedAt;
    return (
      startedAt !== null &&
      performance.now() - startedAt
        < DomestiBotController.CONNECTION_RECONNECT_GRACE_MS
    );
  }

  private endConnectionAssessingAndFlush(): void {
    this.connectionAssessing = false;
    this.connectionAssessingStartedAt = null;
    if (this.bufferedActions.length > 0) {
      void this.flushBufferedActions();
    }
  }

  private async flushBufferedActions(): Promise<void> {
    if (this.bufferFlushInFlight || this.bufferedActions.length === 0) {
      return;
    }
    this.bufferFlushInFlight = true;
    const queue = this.bufferedActions.splice(0);
    try {
      for (const action of queue) {
        await action();
      }
    } finally {
      this.bufferFlushInFlight = false;
      if (this.bufferedActions.length > 0) {
        void this.flushBufferedActions();
      }
    }
  }

  private async handlePollFailure(err: unknown): Promise<void> {
    // Any HTTP response means the server is reachable — keep showing cached
    // tiles even when /v1/ui/state is slow, auth fails, or discovery hiccups.
    if (err instanceof HttpError) {
      this.pollTransportFailureStreak = 0;
      this.markBackendReachableFromCachedSession();
      this.endConnectionAssessingAndFlush();
      return;
    }
    if (!isBackendTransportFailure(err)) {
      this.pollTransportFailureStreak = 0;
      this.markBackendReachableFromCachedSession();
      this.endConnectionAssessingAndFlush();
      return;
    }
    this.pollTransportFailureStreak += 1;
    this.beginConnectionAssessing();
    if (
      this.pollTransportFailureStreak
      < DomestiBotController.POLL_TRANSPORT_FAILURES_BEFORE_VERIFY
    ) {
      return;
    }
    if (this.connectionReconnectGraceActive()) {
      return;
    }
    if (!(await this.verifyBackendUnreachable())) {
      this.markBackendReachableFromCachedSession();
      this.endConnectionAssessingAndFlush();
      return;
    }
    this.markBackendOffline();
  }

  private markBackendOffline(): void {
    this.connected = false;
    this.devicesReady = false;
    this.connectionAssessing = false;
    this.connectionAssessingStartedAt = null;
    this.clearBufferedActions("backend offline");
  }

  private markBackendReachableFromCachedSession(): void {
    if (!this.state || this.lastSuccessfulPollAt === null) {
      return;
    }
    this.connected = true;
    this.devicesReady = true;
  }

  private markBackendReadyFromState(): void {
    // A successful /v1/ui/state implies the backend answered and discovery
    // finished.
    this.pollTransportFailureStreak = 0;
    this.connected = true;
    this.devicesReady = true;
    this.lastSuccessfulPollAt = performance.now();
    this.endConnectionAssessingAndFlush();
  }

  private async runActionNowOrBuffer(
    execute: () => Promise<void>,
  ): Promise<void> {
    if (this.shouldBufferActions()) {
      this.bufferAction(execute);
      return;
    }
    await execute();
  }

  // Shared optimistic tile action: predict next state, render, then run the
  // HTTP call (or queue it during reconnect assessing). On failure, drop the
  // prediction and refresh so the tile shows server truth.
  private async runOptimisticTileAction(
    device: UIDeviceOut,
    nextState: UIDeviceState,
    execute: () => Promise<unknown>,
    onFailure?: (err: unknown) => void | Promise<void>,
  ): Promise<void> {
    this.predictDeviceState(device.family_id, device.id, nextState);
    this.render();
    await this.runActionNowOrBuffer(async () => {
      try {
        await execute();
      } catch (err) {
        this.clearPendingPrediction(device.family_id, device.id);
        if (onFailure !== undefined) {
          await onFailure(err);
        } else {
          console.warn(`[domesti-bot] action on ${device.label} failed`, err);
        }
        await this.refresh();
      }
    });
  }

  private shouldBufferActions(): boolean {
    return this.connectionAssessing;
  }

  private async verifyBackendUnreachable(): Promise<boolean> {
    try {
      await api.fetchHealth();
      this.pollTransportFailureStreak = 0;
      return false;
    } catch (probeErr) {
      if (probeErr instanceof HttpError) {
        this.pollTransportFailureStreak = 0;
        return false;
      }
      if (isBackendTransportFailure(probeErr)) {
        return true;
      }
      this.pollTransportFailureStreak = 0;
      return false;
    }
  }

  private controlsEnabled(): boolean {
    return this.connected && this.devicesReady;
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
    if (
      familyId !== "kasa" &&
      familyId !== "sonos" &&
      familyId !== "tailwind" &&
      familyId !== "vizio"
    ) {
      return;
    }
    if (!this.state) {
      return;
    }
    // Drop optimistic overlays and re-fetch so bulk actions always use
    // the server's view — stale predictions made the pre-check skip the
    // API while tiles still looked active.
    this.clearAllPendingPredictions();
    if (!this.shouldBufferActions()) {
      await this.refresh();
    }
    if (!this.state) {
      return;
    }
    this.predictBulkOffForFamily(familyId);
    this.render();
    await this.runActionNowOrBuffer(async () => {
      try {
        let result: UIBulkActionOut;
        if (familyId === "kasa") {
          result = await api.bulkOffKasa();
        } else if (familyId === "sonos") {
          result = await api.pauseAllSonos();
        } else if (familyId === "vizio") {
          result = await api.bulkOffVizio();
        } else {
          result = await api.closeAllTailwind();
        }
        await this.refresh();
        this.renderBulkActionFeedback(
          familyId,
          result.affected.length,
          0,
          this.state,
        );
      } catch (err) {
        this.clearPendingPredictionsForFamily(familyId);
        this.renderError(`Failed to bulk action on ${familyId}`, err);
      }
    });
  }

  private async onBulkOffGlobal(): Promise<void> {
    if (!this.state) {
      return;
    }
    this.clearAllPendingPredictions();
    if (!this.shouldBufferActions()) {
      await this.refresh();
    }
    if (!this.state) {
      return;
    }
    this.predictBulkOffGlobal();
    this.render();
    await this.runActionNowOrBuffer(async () => {
      try {
        const result = await api.bulkOffGlobal();
        await this.refresh();
        this.renderBulkActionFeedback(
          "global",
          result.affected.length,
          result.skipped.length,
          this.state,
        );
      } catch (err) {
        this.clearAllPendingPredictions();
        this.renderError("Failed to run global all-off", err);
      }
    });
  }

  private async onSetDevicePreferences(
    device: UIDeviceOut,
    excludeFromGlobal: boolean,
    hideOnMobile: boolean,
  ): Promise<void> {
    device.exclude_from_global = excludeFromGlobal;
    device.hide_on_mobile = hideOnMobile;
    await this.runActionNowOrBuffer(async () => {
      try {
        await api.setDevicePreferences(
          device.family_id,
          device.id,
          excludeFromGlobal,
          hideOnMobile,
        );
        await this.refresh();
      } catch (err) {
        device.exclude_from_global = !excludeFromGlobal;
        device.hide_on_mobile = !hideOnMobile;
        this.renderError(
          `Failed to update preference for ${device.label}`,
          err,
        );
        await this.refresh();
      }
    });
  }

  private onSpeakerTileActionFailure(
    device: UIDeviceOut,
    err: unknown,
  ): void {
    if (err instanceof HttpError && err.status === 409) {
      this.renderActionError(err.detail);
    } else {
      console.warn(`[domesti-bot] toggle ${device.label} failed`, err);
    }
  }

  private async onToggleDevice(device: UIDeviceOut): Promise<void> {
    const nextState = nextStateAfterTileToggle(device);
    const onFailure =
      device.kind === UIDeviceKind.Speaker
        ? (err: unknown) => this.onSpeakerTileActionFailure(device, err)
        : undefined;
    await this.runOptimisticTileAction(
      device,
      nextState,
      () => api.toggleDevice(device.family_id, device.id),
      onFailure,
    );
  }

  private operatorAlertStorageKey(alert: UIOperatorAlertOut): string {
    return `domesti-dismissed-operator-alert:${alert.reason_code}:${alert.recorded_at}`;
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

  private createOperatorAlertElement(alert: UIOperatorAlertOut): HTMLElement {
    const banner = document.createElement("aside");
    banner.className = "operator-alert-banner";
    banner.setAttribute("role", "alert");

    const text = document.createElement("p");
    text.className = "operator-alert-message";
    text.textContent = alert.message;

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "operator-alert-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "\u00d7";
    dismiss.addEventListener("click", () => {
      this.dismissOperatorAlert(alert);
      banner.remove();
    });

    banner.append(text, dismiss);
    return banner;
  }

  private predictBulkOffForFamily(familyId: string): void {
    if (!this.state) return;
    for (const family of this.state.families) {
      if (family.id !== familyId) continue;
      for (const device of family.devices) {
        if (!deviceNeedsBulkOff(device)) continue;
        this.predictDeviceState(
          family.id,
          device.id,
          bulkOffStateForKind(device.kind),
        );
      }
    }
  }

  private predictBulkOffGlobal(): void {
    if (!this.state) return;
    for (const family of this.state.families) {
      for (const device of family.devices) {
        if (device.exclude_from_global) continue;
        if (!deviceNeedsBulkOff(device)) continue;
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
    if (this.refreshInFlight) {
      return;
    }
    this.refreshInFlight = true;
    try {
      const state = await api.fetchState();
      this.markBackendReadyFromState();
      this.state = state;
      this.applyPendingPredictionsTo(this.state);
      warmCompactTileIcons(this.state);
      this.render();
    } catch (err) {
      if (this.state) {
        await this.handlePollFailure(err);
        this.render();
      }
    } finally {
      this.refreshInFlight = false;
    }
  }

  private registerCompactLayoutListener(): void {
    const mq = window.matchMedia(COMPACT_LAYOUT_MQ);
    mq.addEventListener("change", () => {
      this.rerenderForLayoutChange();
    });
  }

  private registerCompactTypographyResize(): void {
    registerCompactTypographyResize(this.root);
  }

  private registerVisibilityPollBoost(): void {
    // When the user returns from another app, assume the LAN link is still
    // healthy and queue any taps while we catch up with a fresh poll.
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") {
        return;
      }
      this.beginConnectionAssessing();
      if (this.state !== null) {
        this.render();
      }
      void this.refresh();
    });
  }

  /** Reload tiles from ``/v1/ui/state`` (e.g. after saving settings). */
  async reloadFromServer(): Promise<void> {
    await this.refresh();
  }

  /** Rebuild tiles when crossing the compact layout viewport breakpoint. */
  rerenderForLayoutChange(): void {
    if (this.state === null) {
      return;
    }
    this.render();
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
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    blurFocusedElementInApp(this.root);
    this.root.replaceChildren();
    this.root.dataset["connected"] = this.connected ? "true" : "false";
    this.root.dataset["layout"] = isMobileFormFactor() ? "compact" : "comfortable";
    this.renderOperatorAlert(state.operator_alert);
    if (state.families.length > 0) {
      const header = document.createElement("header");
      header.className = "tile-header tile-header-global";
      const menu = createDesktopMenuButton(this.meta);
      if (menu !== null) {
        header.append(menu);
      }
      header.append(createBrandMark(this.meta));
      appendEp1HeaderStatusStrip(header);
      const actions = document.createElement("div");
      actions.className = "tile-header-actions";
      const globalBtn = createGlobalBulkOffButton();
      globalBtn.disabled = !this.controlsEnabled();
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
      this.restoreScrollAfterRender(scrollX, scrollY);
      return;
    }

    const controlsEnabled = this.controlsEnabled();
    for (const family of state.families) {
      // EP1 is sensors-only and lives in the header status strip (#524), not
      // the family tile grid.
      if (family.id === "ep1") {
        continue;
      }
      this.root.append(
        renderFamily(family, this, this.connected, controlsEnabled),
      );
    }
    this.restoreScrollAfterRender(scrollX, scrollY);
  }

  private dismissActionToast(): void {
    if (this.actionToastTimer !== null) {
      window.clearTimeout(this.actionToastTimer);
      this.actionToastTimer = null;
    }
    if (this.actionToast !== null) {
      this.actionToast.remove();
      this.actionToast = null;
    }
  }

  private dismissOperatorAlert(alert: UIOperatorAlertOut): void {
    try {
      localStorage.setItem(this.operatorAlertStorageKey(alert), "1");
    } catch {
      // Storage may be unavailable (private mode, quota).
    }
  }

  private isOperatorAlertDismissed(alert: UIOperatorAlertOut): boolean {
    try {
      return localStorage.getItem(this.operatorAlertStorageKey(alert)) === "1";
    } catch {
      return false;
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
    this.renderActionToast(message, "error");
  }

  private renderActionToast(
    message: string,
    variant: "error" | "info" | "success",
  ): void {
    this.dismissActionToast();

    const toast = document.createElement("div");
    const variantClass =
      variant === "success"
        ? "action-toast-success"
        : variant === "info"
          ? "action-toast-info"
          : "";
    toast.className =
      variantClass.length > 0
        ? `action-toast ${variantClass}`
        : "action-toast";
    if (variant === "error") {
      toast.setAttribute("role", "alert");
      toast.setAttribute("aria-live", "assertive");
    } else {
      toast.setAttribute("role", "status");
      toast.setAttribute("aria-live", "polite");
    }

    const text = document.createElement("span");
    text.className = "action-toast-message";
    text.textContent = message;

    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "action-toast-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss");
    dismiss.textContent = "\u00d7";
    dismiss.addEventListener("click", () => {
      this.dismissActionToast();
    });

    toast.append(text, dismiss);
    document.body.append(toast);
    this.actionToast = toast;
    this.actionToastTimer = window.setTimeout(() => {
      this.dismissActionToast();
    }, DomestiBotController.ACTION_ERROR_TOAST_MS);
  }

  private renderBulkActionFeedback(
    scope: BulkOffScope,
    affectedCount: number,
    skippedCount: number,
    state: UIStateOut | null,
  ): void {
    if (affectedCount === 0) {
      this.renderActionToast(
        bulkOffNothingChangedMessage(scope, skippedCount, state),
        "info",
      );
      return;
    }
    this.renderActionToast(
      bulkOffSuccessMessage(scope, affectedCount, skippedCount),
      "success",
    );
  }

  private renderError(prefix: string, err: unknown): void {
    const detail = err instanceof HttpError
      ? `${err.status}: ${err.bodyText.slice(0, 200)}`
      : err instanceof Error
        ? err.message
        : String(err);
    this.root.replaceChildren();
    this.root.dataset["layout"] = isMobileFormFactor() ? "compact" : "comfortable";
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
    this.root.dataset["layout"] = isMobileFormFactor() ? "compact" : "comfortable";
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

  private renderOperatorAlert(alert: UIOperatorAlertOut | null | undefined): void {
    if (alert == null) {
      return;
    }
    if (this.isOperatorAlertDismissed(alert)) {
      return;
    }
    this.root.prepend(this.createOperatorAlertElement(alert));
  }

  /** Keep the document scroll position when ``#app`` is rebuilt (mobile Safari jumps to top). */
  private restoreScrollAfterRender(scrollX: number, scrollY: number): void {
    const restore = (): void => {
      window.scrollTo(scrollX, scrollY);
    };
    restore();
    scheduleCompactTypographyFit(this.root, () => {
      restore();
      requestAnimationFrame(restore);
    });
  }

  private static sleep(ms: number): Promise<void> {
    return new Promise((resolve) => {
      window.setTimeout(resolve, ms);
    });
  }

  bulkActionFamilyTile(familyId: string): void {
    void this.onBulkOffFamily(familyId);
  }

  setDevicePreferencesTile(
    device: UIDeviceOut,
    excludeFromGlobal: boolean,
    hideOnMobile: boolean,
  ): void {
    void this.onSetDevicePreferences(device, excludeFromGlobal, hideOnMobile);
  }

  toggleDeviceTile(device: UIDeviceOut): void {
    void this.onToggleDevice(device);
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
  vizio: [
    "M3 6h18v12H3z",
    "M8 20h8",
    "M12 18v2",
  ],
};

/** Static compact-tile icons served from ``/static/icons/compact/<key>.svg``. */
const COMPACT_ICON_BASE = "/static/icons/compact";

/** Prepared markup + cloned DOM per icon key — avoids refetch flicker on every ``render()``. */
const compactIconInflight = new Map<string, Promise<string | null>>();
const compactIconMarkupCache = new Map<string, string>();
const compactIconTemplateCache = new Map<string, HTMLTemplateElement>();

const SVG_NS = "http://www.w3.org/2000/svg";

function applyCompactDefaultTheme(): void {
  if (localStorage.getItem(THEME_STORAGE_KEY) !== null) {
    return;
  }
  if (!window.matchMedia(COMPACT_LAYOUT_MQ).matches) {
    return;
  }
  document.documentElement.setAttribute("data-theme", "dark");
}

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

function blurFocusedElementInApp(appRoot: HTMLElement): void {
  const active = document.activeElement;
  if (active instanceof HTMLElement && appRoot.contains(active)) {
    active.blur();
  }
}

type BulkOffScope = "global" | "kasa" | "sonos" | "tailwind" | "vizio";

function bulkOffAlreadyDoneMessage(scope: BulkOffScope): string {
  switch (scope) {
    case "global":
      return "Everything is already off, paused, or closed.";
    case "kasa":
      return "All lights and plugs are already off.";
    case "sonos":
      return "All Sonos zones are already paused.";
    case "tailwind":
      return "All garage doors are already closed.";
    case "vizio":
      return "All Vizio TVs are already off.";
  }
}

function bulkOffNothingChangedMessage(
  scope: BulkOffScope,
  skippedCount: number,
  state: UIStateOut | null,
): string {
  if (state === null) {
    return bulkOffAlreadyDoneMessage(scope);
  }
  const needingGlobal = countDevicesNeedingBulkOff(state, {
    honorExcludeFromGlobal: true,
  });
  const needingAny = countDevicesNeedingBulkOff(state, {
    honorExcludeFromGlobal: false,
  });
  if (scope === "global" && needingAny > 0 && needingGlobal === 0) {
    const deviceWord = needingAny === 1 ? "device is" : "devices are";
    return `No changes — ${String(needingAny)} ${deviceWord} still active but excluded from global all-off.`;
  }
  if (scope === "global" && skippedCount > 0 && needingGlobal === 0) {
    return bulkOffAlreadyDoneMessage(scope);
  }
  if (needingAny > 0) {
    return "No changes — devices may still be settling; try again in a moment.";
  }
  return bulkOffAlreadyDoneMessage(scope);
}

function bulkOffStateForKind(kind: UIDeviceOut["kind"]): UIDeviceState {
  // What the backend's bulk-off endpoints actually drive each device
  // kind to. Used by the controller's optimistic-prediction helpers
  // so the UI can show the post-action state before the round-trip
  // (and the post-action poll) lands.
  switch (kind) {
    case UIDeviceKind.Switch:
      return UIDeviceState.Off;
    case UIDeviceKind.Speaker:
      return UIDeviceState.Paused;
    case UIDeviceKind.Door:
      return UIDeviceState.Closed;
    case UIDeviceKind.Occupancy:
      // Occupancy tiles are not bulk-off targets; keep the inactive label.
      return UIDeviceState.Clear;
  }
}

function bulkOffSuccessMessage(
  scope: BulkOffScope,
  affectedCount: number,
  skippedCount: number,
): string {
  const deviceWord = affectedCount === 1 ? "device" : "devices";
  const base = ((): string => {
    switch (scope) {
      case "global":
        return `Updated ${String(affectedCount)} ${deviceWord}.`;
      case "kasa":
        return affectedCount === 1
          ? "Turned off 1 light or plug."
          : `Turned off ${String(affectedCount)} lights and plugs.`;
      case "sonos":
        return affectedCount === 1
          ? "Paused 1 zone."
          : `Paused ${String(affectedCount)} zones.`;
      case "tailwind":
        return affectedCount === 1
          ? "Closed 1 garage door."
          : `Closed ${String(affectedCount)} garage doors.`;
      case "vizio":
        return affectedCount === 1
          ? "Turned off 1 TV."
          : `Turned off ${String(affectedCount)} TVs.`;
    }
  })();
  if (scope === "global" && skippedCount > 0) {
    const skipWord = skippedCount === 1 ? "device was" : "devices were";
    return `${base} ${String(skippedCount)} excluded ${skipWord} not changed.`;
  }
  return base;
}

function nextStateAfterTileToggle(device: UIDeviceOut): UIDeviceState {
  // Matches ``compactTileAriaLabel`` — kind drives the next action, not
  // family_id. ``unknown`` follows the same default as the aria hint
  // (switch/speaker: not off/playing → activate; door: close).
  switch (device.kind) {
    case UIDeviceKind.Switch:
      return device.state === UIDeviceState.Off ? UIDeviceState.On : UIDeviceState.Off;
    case UIDeviceKind.Speaker:
      return device.state === UIDeviceState.Playing ? UIDeviceState.Paused : UIDeviceState.Playing;
    case UIDeviceKind.Door:
      return device.state === UIDeviceState.Closed ? UIDeviceState.Open : UIDeviceState.Closed;
    case UIDeviceKind.Occupancy:
      // Sensor-driven; no tile toggle yet — keep current state so optimistic
      // prediction does not briefly flip occupied/clear before BAD_REQUEST.
      return device.state === UIDeviceState.Occupied || device.state === UIDeviceState.Clear
        ? device.state
        : UIDeviceState.Unknown;
  }
}

/** Robot mascot: About entry on mobile; decorative on desktop (use ☰ → About). */
function createBrandMark(meta: MetaOut | null): HTMLElement {
  const wrap = document.createElement("div");
  wrap.className = "brand-mark";
  const mobileAbout = isMobileFormFactor();

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
  if (mobileAbout) {
    const iconBtn = document.createElement("button");
    iconBtn.type = "button";
    iconBtn.className = "brand-mark-icon-btn";
    iconBtn.setAttribute(
      "aria-label",
      "About domesti-bot — show product information",
    );
    iconBtn.append(svg);
    iconBtn.addEventListener("click", () => {
      openAboutDialog(meta);
    });
    wrap.append(iconBtn);
  } else {
    const mascot = document.createElement("span");
    mascot.className = "brand-mark-mascot";
    mascot.setAttribute("aria-hidden", "true");
    mascot.append(svg);
    wrap.append(mascot);
  }
  return wrap;
}

/** Padlock glyph for compact global bulk-off (close doors). */
function createBulkOffPadlockIcon(): SVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "tile-header-global-off-glyph tile-header-global-off-svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "2.2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const shackle = document.createElementNS(SVG_NS, "path");
  shackle.setAttribute("d", "M7 11V8a5 5 0 0 1 10 0v3");
  const body = document.createElementNS(SVG_NS, "rect");
  body.setAttribute("x", "5");
  body.setAttribute("y", "11");
  body.setAttribute("width", "14");
  body.setAttribute("height", "10");
  body.setAttribute("rx", "2");
  svg.append(shackle, body);
  return svg;
}

/** Pause bars for compact global bulk-off (Sonos). */
function createBulkOffPauseIcon(): SVGElement {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "tile-header-global-off-glyph tile-header-global-off-svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "currentColor");
  svg.setAttribute("aria-hidden", "true");
  const left = document.createElementNS(SVG_NS, "rect");
  left.setAttribute("x", "6");
  left.setAttribute("y", "5");
  left.setAttribute("width", "4");
  left.setAttribute("height", "14");
  left.setAttribute("rx", "1");
  const right = document.createElementNS(SVG_NS, "rect");
  right.setAttribute("x", "14");
  right.setAttribute("y", "5");
  right.setAttribute("width", "4");
  right.setAttribute("height", "14");
  right.setAttribute("rx", "1");
  svg.append(left, right);
  return svg;
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

/**
 * Global bulk-off control: full label on comfortable; OFF / pause / padlock +
 * ``all`` on compact so the header keeps room for EP1 readings.
 */
function createGlobalBulkOffButton(): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn btn-bulk tile-header-global-off";
  button.setAttribute("aria-label", GLOBAL_BULK_OFF_LABEL);
  button.title = GLOBAL_BULK_OFF_LABEL;
  if (!isMobileFormFactor()) {
    button.textContent = GLOBAL_BULK_OFF_LABEL;
    return button;
  }
  button.classList.add("tile-header-global-off-icons");
  const off = document.createElement("span");
  off.className = "tile-header-global-off-glyph tile-header-global-off-off";
  off.setAttribute("aria-hidden", "true");
  off.textContent = "OFF";
  const all = document.createElement("span");
  all.className = "tile-header-global-off-glyph tile-header-global-off-all";
  all.setAttribute("aria-hidden", "true");
  all.textContent = "all";
  button.append(off, createBulkOffPauseIcon(), createBulkOffPadlockIcon(), all);
  return button;
}

function createSettingsDialogCloseButton(
  dialog: HTMLDialogElement,
): HTMLButtonElement {
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "settings-dialog-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "\u00d7";
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  return closeBtn;
}

let appMenuOpen = false;
let openAppMenuCloser: (() => void) | null = null;

function closeAppMenu(): void {
  appMenuOpen = false;
  if (openAppMenuCloser !== null) {
    openAppMenuCloser();
    openAppMenuCloser = null;
  }
}

let compactTypographyFitFrame = 0;
let compactTypographyResizeObserver: ResizeObserver | null = null;

function compactBulkButtonFitsAtSize(
  appRoot: HTMLElement,
  button: HTMLElement,
  fontPx: number,
): boolean {
  appRoot.style.setProperty("--compact-global-bulk-px", `${fontPx}px`);
  return (
    button.scrollHeight <= button.clientHeight + 1
    && button.scrollWidth <= button.clientWidth + 1
  );
}

function compactLabelFitsAtSize(
  appRoot: HTMLElement,
  labels: readonly HTMLElement[],
  fontPx: number,
): boolean {
  appRoot.style.setProperty("--compact-tile-label-px", `${fontPx}px`);
  return labels.every((label) => {
    return (
      label.scrollHeight <= label.clientHeight + 1
      && label.scrollWidth <= label.clientWidth + 1
    );
  });
}

function countDevicesNeedingBulkOff(
  state: UIStateOut,
  options: {
    familyId?: string;
    honorExcludeFromGlobal: boolean;
  },
): number {
  let count = 0;
  for (const family of state.families) {
    if (options.familyId !== undefined && family.id !== options.familyId) {
      continue;
    }
    for (const device of family.devices) {
      if (options.honorExcludeFromGlobal && device.exclude_from_global) {
        continue;
      }
      if (deviceNeedsBulkOff(device)) {
        count += 1;
      }
    }
  }
  return count;
}

function largestCompactBulkFontPx(
  appRoot: HTMLElement,
  button: HTMLElement,
): number {
  let lo = COMPACT_BULK_FONT_MIN_PX;
  let hi = COMPACT_BULK_FONT_MAX_PX;
  let best = lo;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (compactBulkButtonFitsAtSize(appRoot, button, mid)) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best;
}

function largestCompactLabelFontPx(
  appRoot: HTMLElement,
  labels: readonly HTMLElement[],
): number | null {
  if (labels.length === 0) {
    return null;
  }
  let lo = COMPACT_LABEL_FONT_MIN_PX;
  let hi = COMPACT_LABEL_FONT_MAX_PX;
  let best = lo;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (compactLabelFitsAtSize(appRoot, labels, mid)) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best;
}

function registerCompactTypographyResize(appRoot: HTMLElement): void {
  if (compactTypographyResizeObserver !== null) {
    return;
  }
  compactTypographyResizeObserver = new ResizeObserver(() => {
    scheduleCompactTypographyFit(appRoot);
  });
  compactTypographyResizeObserver.observe(appRoot);
}

function scheduleCompactTypographyFit(
  appRoot: HTMLElement,
  afterFit?: () => void,
): void {
  if (compactTypographyFitFrame !== 0) {
    cancelAnimationFrame(compactTypographyFitFrame);
  }
  compactTypographyFitFrame = requestAnimationFrame(() => {
    compactTypographyFitFrame = requestAnimationFrame(() => {
      compactTypographyFitFrame = 0;
      syncCompactTypographyFit(appRoot);
      afterFit?.();
    });
  });
}

function syncCompactTypographyFit(appRoot: HTMLElement): void {
  if (!isMobileFormFactor()) {
    appRoot.style.removeProperty("--compact-tile-label-px");
    appRoot.style.removeProperty("--compact-global-bulk-px");
    return;
  }
  const labels = [
    ...appRoot.querySelectorAll<HTMLElement>(".tile-compact .tile-saturated-label"),
  ];
  const labelPx = largestCompactLabelFontPx(appRoot, labels);
  if (labelPx !== null) {
    appRoot.style.setProperty("--compact-tile-label-px", `${labelPx}px`);
  }
  const bulkBtn = appRoot.querySelector<HTMLElement>(".tile-header-global-off");
  if (bulkBtn !== null) {
    const bulkPx = largestCompactBulkFontPx(appRoot, bulkBtn);
    appRoot.style.setProperty("--compact-global-bulk-px", `${bulkPx}px`);
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
  let outsideClickListener: ((ev: MouseEvent) => void) | null = null;

  const detachOutsideClickListener = (): void => {
    if (outsideClickListener !== null) {
      document.removeEventListener("click", outsideClickListener, true);
      outsideClickListener = null;
    }
  };

  const runMenuItemAction = (action: () => void): void => {
    detachOutsideClickListener();
    panel.hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    appMenuOpen = false;
    openAppMenuCloser = null;
    action();
  };
  settingsItem.addEventListener("click", (ev) => {
    ev.stopPropagation();
    runMenuItemAction(() => {
      void openSettingsHubDialog({
        onReloadDevices: () => domestiUiController?.reloadFromServer(),
      });
    });
  });
  const rulesItem = document.createElement("button");
  rulesItem.type = "button";
  rulesItem.className = "app-menu-item";
  rulesItem.setAttribute("role", "menuitem");
  rulesItem.textContent = "Automations";
  rulesItem.addEventListener("click", (ev) => {
    ev.stopPropagation();
    runMenuItemAction(() => {
      void openAutomationsHubDialog();
    });
  });
  const aboutItem = document.createElement("button");
  aboutItem.type = "button";
  aboutItem.className = "app-menu-item";
  aboutItem.setAttribute("role", "menuitem");
  aboutItem.textContent = "About";
  aboutItem.addEventListener("click", (ev) => {
    ev.stopPropagation();
    runMenuItemAction(() => {
      openAboutDialog(meta);
    });
  });
  panel.append(rulesItem, settingsItem, aboutItem);
  wrap.append(trigger, panel);

  const openPanel = (): void => {
    panel.hidden = false;
    trigger.setAttribute("aria-expanded", "true");
    appMenuOpen = true;
    detachOutsideClickListener();
    outsideClickListener = (ev: MouseEvent): void => {
      if (!wrap.contains(ev.target as Node)) {
        closeAppMenu();
      }
    };
    window.setTimeout(() => {
      if (outsideClickListener !== null) {
        document.addEventListener("click", outsideClickListener, true);
      }
    }, 0);
    openAppMenuCloser = () => {
      panel.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      detachOutsideClickListener();
    };
  };

  trigger.addEventListener("click", (ev) => {
    ev.stopPropagation();
    if (panel.hidden) {
      openPanel();
    } else {
      closeAppMenu();
    }
  });

  if (appMenuOpen) {
    openPanel();
  }

  return wrap;
}

function openAboutDialog(meta: MetaOut | null): void {
  const dialog = document.createElement("dialog");
  dialog.className = "settings-dialog about-dialog";
  const panel = document.createElement("div");
  panel.className = "settings-dialog-panel";
  const header = document.createElement("header");
  header.className = "settings-dialog-header";
  const title = document.createElement("h2");
  title.textContent = "About domesti-bot";
  header.append(title, createSettingsDialogCloseButton(dialog));
  const body = document.createElement("div");
  body.className = "settings-dialog-body";
  appendAboutContent(body, meta);
  panel.append(header, body);
  dialog.append(panel);
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

function appendAboutContent(body: HTMLElement, meta: MetaOut | null): void {
  const tagline = document.createElement("p");
  tagline.className = "settings-dialog-lead";
  tagline.textContent = ABOUT_TAGLINE;
  const copyright = document.createElement("p");
  copyright.className = "about-dialog-meta";
  copyright.textContent = ABOUT_COPYRIGHT;
  const license = document.createElement("p");
  license.className = "about-dialog-meta";
  license.textContent = ABOUT_LICENSE;
  const version = document.createElement("p");
  version.className = "settings-dialog-status";
  version.textContent = meta
    ? `Version ${meta.version} · commit ${meta.commit}`
    : "Loading build info\u2026";
  const repo = document.createElement("a");
  repo.className = "about-dialog-repo";
  repo.href = DOMESTI_BOT_REPO_HREF;
  repo.target = "_blank";
  repo.rel = "noopener noreferrer";
  repo.textContent = ABOUT_REPO_LABEL;
  body.append(tagline, copyright, license, version, repo);
}

/**
 * Insert the EP1 climate / light strip into the dashboard header.
 *
 * TODO(ep1-header-live): pass ``ep1HeaderStatusFromUiState(state)`` with
 * ``mock: false`` once an EP1 is on the LAN; drop ``MOCK_EP1_HEADER_STATUS``.
 */
function appendEp1HeaderStatusStrip(header: HTMLElement): void {
  const strip = createEp1HeaderStatusStrip(MOCK_EP1_HEADER_STATUS, {
    mock: true,
  });
  if (strip !== null) {
    header.append(strip);
  }
}

function appendSaturatedTileVisuals(
  container: HTMLElement,
  device: UIDeviceOut,
  compactHalfLayout: boolean,
): void {
  const iconWrap = document.createElement("span");
  iconWrap.className = "tile-saturated-icon-wrap";
  iconWrap.append(createTileIcon(device));
  container.append(iconWrap);

  const label = document.createElement("span");
  label.className = "tile-saturated-label";
  label.textContent = device.label;

  const stateCaption = compactHalfLayout ? null : tileStateCaption(device);
  const stateEl =
    stateCaption !== null ? document.createElement("span") : null;
  if (stateEl !== null) {
    stateEl.className = "tile-saturated-state";
    stateEl.textContent = stateCaption;
  }

  if (compactHalfLayout) {
    const textZone = document.createElement("div");
    textZone.className = "tile-saturated-text";
    textZone.append(label);
    if (stateEl !== null) {
      textZone.append(stateEl);
    }
    container.append(textZone);
    return;
  }

  container.append(label);
  if (stateEl !== null) {
    container.append(stateEl);
  }
}

function attachTileHitListeners(
  hit: HTMLButtonElement,
  device: UIDeviceOut,
  controller: DomestiBotController,
): void {
  let longPressTimer: number | null = null;
  let suppressClick = false;
  let startX = 0;
  let startY = 0;

  const clearLongPress = (): void => {
    if (longPressTimer !== null) {
      window.clearTimeout(longPressTimer);
      longPressTimer = null;
    }
  };

  hit.addEventListener("click", (ev) => {
    if (suppressClick) {
      ev.preventDefault();
      ev.stopPropagation();
      suppressClick = false;
      return;
    }
    controller.toggleDeviceTile(device);
  });

  hit.addEventListener("contextmenu", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    openDevicePropertiesMenu(device, controller, ev.clientX, ev.clientY);
  });

  hit.addEventListener("pointerdown", (ev) => {
    if (ev.pointerType === "mouse" && ev.button !== 0) {
      return;
    }
    // Start each gesture clean so a prior long-press that never got a
    // click (pointercancel / lift-off) cannot permanently swallow taps.
    suppressClick = false;
    startX = ev.clientX;
    startY = ev.clientY;
    clearLongPress();
    longPressTimer = window.setTimeout(() => {
      longPressTimer = null;
      suppressClick = true;
      openDevicePropertiesMenu(device, controller, startX, startY);
    }, DEVICE_PROPERTIES_LONG_PRESS_MS);
  });

  hit.addEventListener("pointermove", (ev) => {
    if (longPressTimer === null) {
      return;
    }
    const dx = ev.clientX - startX;
    const dy = ev.clientY - startY;
    if (dx * dx + dy * dy > 100) {
      clearLongPress();
    }
  });

  hit.addEventListener("pointerup", clearLongPress);
  hit.addEventListener("pointercancel", clearLongPress);
  hit.addEventListener("pointerleave", clearLongPress);
}

function compactTileAriaLabel(device: UIDeviceOut): string {
  const statePhrase =
    device.state === UIDeviceState.Unknown ? "state unknown" : `currently ${device.state}`;
  switch (device.kind) {
    case UIDeviceKind.Switch: {
      const next = device.state === UIDeviceState.Off ? "turn on" : "turn off";
      return `${device.label}, ${statePhrase}, tap to ${next}`;
    }
    case UIDeviceKind.Speaker: {
      const next = device.state === UIDeviceState.Playing ? "pause" : "resume";
      return `${device.label}, ${statePhrase}, tap to ${next}`;
    }
    case UIDeviceKind.Door: {
      const next =
        device.state === UIDeviceState.Open
          ? "close"
          : device.state === UIDeviceState.Closed
            ? "open"
            : "close";
      return `${device.label}, ${statePhrase}, tap to ${next}`;
    }
    case UIDeviceKind.Occupancy: {
      // Sensor-only for now (no tile toggle); omit actionable "tap to …".
      const temp = formatOccupancyTemperatureDual(device.occupancy_readings);
      return temp == null
        ? `${device.label}, ${statePhrase}, occupancy sensor`
        : `${device.label}, ${statePhrase}, occupancy sensor, ${temp}`;
    }
  }
}

function compactIconAssetKey(device: UIDeviceOut): string {
  if (device.compact_icon === "garage" || device.kind === UIDeviceKind.Door) {
    return device.state === UIDeviceState.Open ? "garage_open" : "garage_closed";
  }
  if (
    device.compact_icon === "occupancy" ||
    device.kind === UIDeviceKind.Occupancy ||
    device.family_id === "ep1"
  ) {
    return "occupancy";
  }
  if (
    device.compact_icon === "tv" ||
    device.family_id === "vizio"
  ) {
    if (device.state === UIDeviceState.Unknown) {
      return "tv_off";
    }
    return device.state === UIDeviceState.On ? "tv_on" : "tv_off";
  }
  if (
    device.compact_icon === "speaker" ||
    device.kind === UIDeviceKind.Speaker ||
    device.family_id === "sonos"
  ) {
    if (device.state === UIDeviceState.Playing) {
      return "speaker_playing";
    }
    if (device.state === UIDeviceState.Unknown) {
      return "speaker_unknown";
    }
    return "speaker_paused";
  }
  return device.compact_icon;
}

function compactIconAssetUrl(key: string): string {
  return `${COMPACT_ICON_BASE}/${key}.svg`;
}

function compactIconCacheCandidates(key: string): string[] {
  if (key.startsWith("speaker_")) {
    return [key, "speaker"];
  }
  if (key.startsWith("garage_")) {
    return [key, "garage_closed"];
  }
  if (key.startsWith("tv_")) {
    return key === "tv_on" ? [key, "tv_off"] : [key, "tv_on"];
  }
  return [key];
}

function compactIconFallbackCandidates(key: string): string[] {
  if (key.startsWith("speaker_")) {
    return [key, "speaker", "bulb"];
  }
  if (key.startsWith("garage_")) {
    return [key, "garage_closed", "bulb"];
  }
  if (key.startsWith("tv_")) {
    return [key, "tv_off", "bulb"];
  }
  if (key === "tv") {
    return ["tv_off", "tv_on", "bulb"];
  }
  return [key, "bulb"];
}

function applyCompactIconMarkupToHost(
  host: HTMLSpanElement,
  markup: string,
  key: string,
): void {
  rememberCompactIconMarkup(key, markup);
  mountCompactIconFromCache(host, key);
}

function createTileIcon(device: UIDeviceOut): HTMLSpanElement {
  const host = document.createElement("span");
  host.className = "tile-saturated-icon-host";
  const key = compactIconAssetKey(device);
  if (!mountCompactIconFromCache(host, key)) {
    void loadCompactTileIconInto(host, key);
  }
  return host;
}

async function fetchAndCacheCompactIconMarkup(
  candidate: string,
): Promise<string | null> {
  const cached = compactIconMarkupCache.get(candidate);
  if (cached !== undefined) {
    return cached;
  }
  const inflight = compactIconInflight.get(candidate);
  if (inflight !== undefined) {
    return inflight;
  }
  const promise = (async (): Promise<string | null> => {
    try {
      const response = await fetch(compactIconAssetUrl(candidate));
      if (!response.ok) {
        return null;
      }
      const markup = prepareCompactIconMarkup(await response.text());
      if (!markup.includes("<svg")) {
        return null;
      }
      rememberCompactIconMarkup(candidate, markup);
      return markup;
    } catch {
      return null;
    } finally {
      compactIconInflight.delete(candidate);
    }
  })();
  compactIconInflight.set(candidate, promise);
  return promise;
}

async function loadCompactTileIconInto(
  host: HTMLSpanElement,
  key: string,
): Promise<void> {
  for (const candidate of compactIconCacheCandidates(key)) {
    if (mountCompactIconFromCache(host, candidate)) {
      return;
    }
  }
  for (const candidate of compactIconFallbackCandidates(key)) {
    const markup = await fetchAndCacheCompactIconMarkup(candidate);
    if (markup !== null) {
      if (host.isConnected) {
        applyCompactIconMarkupToHost(host, markup, candidate);
      }
      return;
    }
  }
}

function mountCompactIconFromCache(host: HTMLSpanElement, key: string): boolean {
  const template = compactIconTemplateCache.get(key);
  if (template === undefined) {
    const markup = compactIconMarkupCache.get(key);
    if (markup === undefined) {
      return false;
    }
    rememberCompactIconMarkup(key, markup);
    return mountCompactIconFromCache(host, key);
  }
  host.replaceChildren(template.content.cloneNode(true));
  host.dataset.iconKey = key;
  return true;
}

function prepareCompactIconMarkup(raw: string): string {
  let snippet = raw.replace(/<\?xml[^>]*\?>\s*/, "").trim();
  if (!snippet.includes('class="tile-saturated-icon"')) {
    snippet = snippet.replace(
      /<svg\b/,
      '<svg class="tile-saturated-icon" aria-hidden="true"',
    );
  }
  return snippet;
}

function rememberCompactIconMarkup(key: string, markup: string): void {
  compactIconMarkupCache.set(key, markup);
  const template = document.createElement("template");
  template.innerHTML = markup;
  compactIconTemplateCache.set(key, template);
}

function warmCompactTileIcons(state: UIStateOut): void {
  const keys = new Set<string>([
    "bulb",
    "speaker",
    "speaker_playing",
    "speaker_paused",
    "speaker_unknown",
    "garage_open",
    "garage_closed",
    "tv_on",
    "tv_off",
  ]);
  for (const family of state.families) {
    if (family.id === "vizio") {
      keys.add("tv_on");
      keys.add("tv_off");
    }
    for (const device of family.devices) {
      keys.add(compactIconAssetKey(device));
    }
  }
  for (const key of keys) {
    if (compactIconMarkupCache.has(key)) {
      continue;
    }
    void fetchAndCacheCompactIconMarkup(key);
  }
}

function closeDevicePropertiesMenu(): void {
  document.getElementById("device-properties-menu")?.remove();
}

function createDevicePropertiesMenuItem(
  label: string,
  help: string,
  checked: boolean,
  onToggle: () => void,
): HTMLButtonElement {
  const item = document.createElement("button");
  item.type = "button";
  item.className = "device-properties-menu-item";
  item.setAttribute("role", "menuitemcheckbox");
  item.setAttribute("aria-checked", checked ? "true" : "false");
  item.title = help;
  const mark = document.createElement("span");
  mark.className = "device-properties-menu-check";
  mark.textContent = checked ? "✓" : "";
  mark.setAttribute("aria-hidden", "true");
  const text = document.createElement("span");
  text.textContent = label;
  item.append(mark, text);
  item.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    closeDevicePropertiesMenu();
    onToggle();
  });
  return item;
}

function createTilePrefsBadge(device: UIDeviceOut): HTMLSpanElement | null {
  if (!device.exclude_from_global && !device.hide_on_mobile) {
    return null;
  }
  const parts: string[] = [];
  if (device.exclude_from_global) {
    parts.push("excluded from bulk");
  }
  if (device.hide_on_mobile) {
    parts.push("hidden on phone / tablet");
  }
  const badge = document.createElement("span");
  badge.className = "tile-prefs-badge";
  badge.textContent = "prefs";
  badge.title = parts.join("; ");
  return badge;
}

function deviceIdentityTooltip(device: UIDeviceOut): string {
  const lines = [DEVICE_PROPERTIES_MENU_HINT, ""];
  lines.push(`MAC address: ${device.mac_address}`);
  const host = (device.host ?? "").trim();
  if (host) {
    lines.push(`IP: ${host}`);
  }
  for (const detail of device.identity_details ?? []) {
    const text = detail.trim();
    if (text) {
      lines.push(text);
    }
  }
  return lines.join("\n");
}

function createTileSaturatedHit(
  device: UIDeviceOut,
  controller: DomestiBotController,
  connected: boolean,
  hitClassName: string,
): HTMLButtonElement {
  const hit = document.createElement("button");
  hit.type = "button";
  hit.className = hitClassName;
  hit.dataset["tone"] = deviceStateTone(device.state);
  const isActive =
    device.state === UIDeviceState.On ||
    device.state === UIDeviceState.Playing ||
    device.state === UIDeviceState.Open ||
    device.state === UIDeviceState.Occupied;
  hit.setAttribute("aria-pressed", isActive ? "true" : "false");
  hit.setAttribute("aria-label", compactTileAriaLabel(device));
  hit.title = deviceIdentityTooltip(device);
  hit.disabled = !connected;
  appendSaturatedTileVisuals(hit, device, hitClassName === "tile-compact-hit");
  attachTileHitListeners(hit, device, controller);
  return hit;
}

function deviceNeedsBulkOff(device: UIDeviceOut): boolean {
  switch (device.kind) {
    case UIDeviceKind.Switch:
      return device.state === UIDeviceState.On;
    case UIDeviceKind.Speaker:
      // Match doors: unknown may still be playing before the first poll.
      return device.state === UIDeviceState.Playing || device.state === UIDeviceState.Unknown;
    case UIDeviceKind.Door:
      return device.state === UIDeviceState.Open || device.state === UIDeviceState.Unknown;
    case UIDeviceKind.Occupancy:
      return false;
  }
}

function deviceStateTone(state: UIDeviceState): "active" | "inactive" | "unknown" {
  if (state === UIDeviceState.Unknown) {
    return "unknown";
  }
  if (
    state === UIDeviceState.On ||
    state === UIDeviceState.Playing ||
    state === UIDeviceState.Open ||
    state === UIDeviceState.Occupied
  ) {
    return "active";
  }
  return "inactive";
}

/** Dual-unit temperature for occupancy aria (· matches header metric separators). */
function formatOccupancyTemperatureDual(
  readings: UIOccupancyReadingsOut | null | undefined,
): string | null {
  if (readings == null) {
    return null;
  }
  let celsius = readings.temperature_c;
  let fahrenheit = readings.temperature_f;
  if (celsius == null && fahrenheit != null) {
    celsius = ((fahrenheit - 32) * 5) / 9;
  }
  if (fahrenheit == null && celsius != null) {
    fahrenheit = (celsius * 9) / 5 + 32;
  }
  if (celsius == null || fahrenheit == null) {
    return null;
  }
  return `${celsius.toFixed(1)} °C · ${fahrenheit.toFixed(1)} °F`;
}

function isMobileFormFactor(): boolean {
  // Viewport width is the single source of truth. ``userAgentData.mobile``
  // is often ``false`` in desktop devtools and some in-app browsers even
  // when the viewport is phone-sized, which previously left ``data-layout``
  // on ``comfortable`` (single column, gray ``.tile`` chrome).
  return window.matchMedia(COMPACT_LAYOUT_MQ).matches;
}

function openDevicePropertiesMenu(
  device: UIDeviceOut,
  controller: DomestiBotController,
  clientX: number,
  clientY: number,
): void {
  closeDevicePropertiesMenu();
  const menu = document.createElement("div");
  menu.id = "device-properties-menu";
  menu.className = "device-properties-menu";
  menu.setAttribute("role", "menu");
  menu.setAttribute("aria-label", "Device properties");
  menu.append(
    createDevicePropertiesMenuItem(
      DEVICE_PROPERTIES_EXCLUDE_LABEL,
      DEVICE_PROPERTIES_EXCLUDE_HELP,
      device.exclude_from_global,
      () => {
        controller.setDevicePreferencesTile(
          device,
          !device.exclude_from_global,
          device.hide_on_mobile,
        );
      },
    ),
    createDevicePropertiesMenuItem(
      DEVICE_PROPERTIES_HIDE_LABEL,
      DEVICE_PROPERTIES_HIDE_HELP,
      device.hide_on_mobile,
      () => {
        controller.setDevicePreferencesTile(
          device,
          device.exclude_from_global,
          !device.hide_on_mobile,
        );
      },
    ),
  );
  document.body.append(menu);
  const pad = 8;
  const rect = menu.getBoundingClientRect();
  const left = Math.min(
    Math.max(pad, clientX),
    window.innerWidth - rect.width - pad,
  );
  const top = Math.min(
    Math.max(pad, clientY),
    window.innerHeight - rect.height - pad,
  );
  menu.style.left = `${left}px`;
  menu.style.top = `${top}px`;

  const dismiss = (ev: Event): void => {
    if (ev.target instanceof Node && menu.contains(ev.target)) {
      return;
    }
    closeDevicePropertiesMenu();
    window.removeEventListener("pointerdown", dismiss, true);
    window.removeEventListener("keydown", onKey, true);
  };
  const onKey = (ev: KeyboardEvent): void => {
    if (ev.key === "Escape") {
      closeDevicePropertiesMenu();
      window.removeEventListener("pointerdown", dismiss, true);
      window.removeEventListener("keydown", onKey, true);
    }
  };
  window.setTimeout(() => {
    window.addEventListener("pointerdown", dismiss, true);
    window.addEventListener("keydown", onKey, true);
  }, 0);
}

function renderDevice(
  device: UIDeviceOut,
  controller: DomestiBotController,
  connected: boolean,
): HTMLElement {
  if (isMobileFormFactor()) {
    return renderDeviceCompact(device, controller, connected);
  }
  return renderDeviceComfortable(device, controller, connected);
}

function renderDeviceComfortable(
  device: UIDeviceOut,
  controller: DomestiBotController,
  connected: boolean,
): HTMLElement {
  const tile = document.createElement("article");
  tile.className = `tile-rich tile-${device.kind}`;
  tile.dataset["familyId"] = device.family_id;
  tile.dataset["deviceId"] = device.id;
  tile.dataset["state"] = device.state;

  tile.append(createTileSaturatedHit(device, controller, connected, "tile-rich-hit"));
  const badge = createTilePrefsBadge(device);
  if (badge !== null) {
    tile.append(badge);
  }
  return tile;
}

function renderDeviceCompact(
  device: UIDeviceOut,
  controller: DomestiBotController,
  connected: boolean,
): HTMLElement {
  const tile = document.createElement("article");
  // Do not add the comfortable ``.tile`` class — its card chrome (gray
  // background, colored left border) bleeds through when ``data-layout``
  // is stale or CSS is cached.
  tile.className = `tile-compact tile-${device.kind}`;
  tile.dataset["familyId"] = device.family_id;
  tile.dataset["deviceId"] = device.id;
  tile.dataset["state"] = device.state;

  tile.append(createTileSaturatedHit(device, controller, connected, "tile-compact-hit"));
  return tile;
}

function renderFamily(
  family: UIFamilyOut,
  controller: DomestiBotController,
  backendConnected: boolean,
  controlsEnabled: boolean,
): HTMLElement {
  const section = document.createElement("section");
  section.className = "family";
  section.dataset["familyId"] = family.id;
  section.dataset["connected"] = backendConnected ? "true" : "false";
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
    family.id === "tailwind" ||
    family.id === "vizio"
  ) {
    const bulkBtn = document.createElement("button");
    bulkBtn.type = "button";
    // Per-family bulk-off ("Turn off all" / "Pause all" / "Close all") uses
    // ``btn-bulk`` (warm orange) so multi-device actions read separately from
    // per-tile ``btn-danger`` toggles and from green ``--accent`` / amber
    // ``--pending`` state badges.
    bulkBtn.className = "btn btn-bulk";
    bulkBtn.textContent =
      family.id === "kasa" || family.id === "vizio"
        ? "Turn off all"
        : family.id === "sonos"
          ? "Pause all"
          : "Close all";
    bulkBtn.disabled = !controlsEnabled;
    bulkBtn.addEventListener("click", () => {
      controller.bulkActionFamilyTile(family.id);
    });
    header.append(bulkBtn);
  }
  section.append(header);

  const grid = document.createElement("div");
  grid.className = isMobileFormFactor()
    ? "tile-grid tile-grid-compact"
    : "tile-grid";
  const compact = isMobileFormFactor();
  for (const device of family.devices) {
    if (compact && device.hide_on_mobile) {
      continue;
    }
    grid.append(renderDevice(device, controller, controlsEnabled));
  }
  section.append(grid);
  return section;
}

function tileStateCaption(device: UIDeviceOut): string | null {
  if (device.state === UIDeviceState.Unknown) {
    return "Unknown";
  }
  if (device.kind === UIDeviceKind.Switch) {
    return device.state === UIDeviceState.On ? "On" : "Off";
  }
  return device.state.charAt(0).toUpperCase() + device.state.slice(1);
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

function initAutomationsDeepLinks(): void {
  let opening: Promise<void> | null = null;
  const openFromHash = (): void => {
    const link = parseAutomationsDeepLink(window.location.hash);
    if (link === null) {
      return;
    }
    if (
      opening !== null
      || document.querySelector("dialog.automations-dialog[open]") !== null
    ) {
      return;
    }
    opening = openAutomationsHubDialog(link).finally(() => {
      opening = null;
    });
  };
  openFromHash();
  window.addEventListener("hashchange", openFromHash);
}

function start(): void {
  removeJsBootHint();
  applyCompactDefaultTheme();
  applyStoredColorTheme();
  initPwaInstallBanner();
  registerServiceWorker();
  initAutomationsDeepLinks();
  const root = document.getElementById(APP_ROOT_ID);
  if (!root) {
    console.warn(`[domesti-bot] expected #${APP_ROOT_ID} in landing page`);
    return;
  }
  const controller = new DomestiBotController(root);
  domestiUiController = controller;
  void controller.init();
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", start, { once: true });
} else {
  start();
}
