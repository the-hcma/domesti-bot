// Dismissible toasts and styled confirm prompts (replaces window.alert/confirm).

import { ConfirmButtonVariant, ToastVariant } from "./closed-sets.js";
export { ToastVariant } from "./closed-sets.js";

const DEFAULT_TOAST_MS = 10_000;
const SUCCESS_TOAST_MS = 5_000;

let activeToast: HTMLDivElement | null = null;
let activeToastTimer: number | null = null;

/** CSS class list for an action toast of the given variant (always includes an explicit tone class). */
export function actionToastClassName(variant: ToastVariant): string {
  if (variant === ToastVariant.Error) {
    return "action-toast action-toast-error";
  }
  if (variant === ToastVariant.Success) {
    return "action-toast action-toast-success";
  }
  return "action-toast action-toast-info";
}

export function confirmAction(options: {
  cancelLabel?: string;
  confirmLabel?: string;
  message: string;
  title?: string;
  variant?: ConfirmButtonVariant;
}): Promise<boolean> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "settings-dialog ui-confirm-dialog";

    const panel = document.createElement("div");
    panel.className = "settings-dialog-panel";

    const body = document.createElement("div");
    body.className = "settings-dialog-body ui-confirm-body";

    if (options.title !== undefined && options.title !== "") {
      const title = document.createElement("h2");
      title.className = "ui-confirm-title";
      title.textContent = options.title;
      body.append(title);
    }

    const message = document.createElement("p");
    message.className = "settings-dialog-lead ui-confirm-message";
    message.textContent = options.message;
    body.append(message);

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions ui-confirm-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-secondary";
    cancelBtn.textContent = options.cancelLabel ?? "Cancel";
    const confirmBtn = document.createElement("button");
    confirmBtn.type = "button";
    confirmBtn.className =
      options.variant === ConfirmButtonVariant.Danger ? "btn btn-danger" : "btn";
    confirmBtn.textContent = options.confirmLabel ?? "Confirm";
    actions.append(cancelBtn, confirmBtn);
    body.append(actions);
    panel.append(body);
    dialog.append(panel);

    const finish = (confirmed: boolean): void => {
      dialog.close();
      dialog.remove();
      resolve(confirmed);
    };

    cancelBtn.addEventListener("click", () => {
      finish(false);
    });
    confirmBtn.addEventListener("click", () => {
      finish(true);
    });
    dialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      finish(false);
    });
    dialog.addEventListener("click", (ev) => {
      if (ev.target === dialog) {
        finish(false);
      }
    });

    document.body.append(dialog);
    dialog.showModal();
    confirmBtn.focus();
  });
}

export function showErrorToast(message: string): void {
  showToast(message, ToastVariant.Error);
}

export function showInfoToast(message: string): void {
  showToast(message, ToastVariant.Info);
}

export function showSuccessToast(message: string): void {
  showToast(message, ToastVariant.Success);
}

export function dismissActiveToast(): void {
  dismissToast();
}

const DISCOVERY_PROGRESS_POLL_MS = 200;

let discoveryProgressTimer: number | null = null;

/** Persistent info toast with optional elapsed/estimate progress (Settings discovery). */
export function showDiscoveryProgressToast(
  message: string,
  estimatedMs: number | null,
): () => void {
  dismissToast();
  if (discoveryProgressTimer !== null) {
    window.clearInterval(discoveryProgressTimer);
    discoveryProgressTimer = null;
  }

  const toast = document.createElement("div");
  toast.className = `${actionToastClassName(ToastVariant.Info)} action-toast-persistent`;

  const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  icon.setAttribute("class", "action-toast-clock-icon");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  const circle = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  circle.setAttribute("cx", "12");
  circle.setAttribute("cy", "12");
  circle.setAttribute("r", "9");
  circle.setAttribute("fill", "none");
  circle.setAttribute("stroke", "currentColor");
  circle.setAttribute("stroke-width", "2");
  const hand = document.createElementNS("http://www.w3.org/2000/svg", "line");
  hand.setAttribute("class", "action-toast-clock-hand");
  hand.setAttribute("x1", "12");
  hand.setAttribute("y1", "12");
  hand.setAttribute("x2", "12");
  hand.setAttribute("y2", "7");
  hand.setAttribute("stroke", "currentColor");
  hand.setAttribute("stroke-width", "2");
  hand.setAttribute("stroke-linecap", "round");
  icon.append(circle, hand);

  const body = document.createElement("div");
  body.className = "action-toast-body";

  const text = document.createElement("span");
  text.className = "action-toast-message";
  text.textContent = message;

  body.append(text);

  let progressBar: HTMLDivElement | null = null;
  if (estimatedMs !== null && estimatedMs > 0) {
    const track = document.createElement("div");
    track.className = "discovery-progress-track";
    track.setAttribute("role", "progressbar");
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", "0");
    progressBar = document.createElement("div");
    progressBar.className = "discovery-progress-bar";
    track.append(progressBar);
    body.append(track);
  }

  toast.append(icon, body);
  toast.setAttribute("role", "status");
  toast.setAttribute("aria-live", "polite");
  document.body.append(toast);
  activeToast = toast;

  if (estimatedMs !== null && estimatedMs > 0 && progressBar !== null) {
    const track = progressBar.parentElement;
    const started = performance.now();
    discoveryProgressTimer = window.setInterval(() => {
      const elapsed = performance.now() - started;
      const ratio = Math.min(1, elapsed / estimatedMs);
      progressBar!.style.width = `${Math.round(ratio * 100)}%`;
      track?.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    }, DISCOVERY_PROGRESS_POLL_MS);
  }

  return () => {
    if (discoveryProgressTimer !== null) {
      window.clearInterval(discoveryProgressTimer);
      discoveryProgressTimer = null;
    }
    dismissToast();
  };
}

export function showToast(
  message: string,
  variant: ToastVariant = ToastVariant.Info,
  durationMs?: number,
): void {
  dismissToast();

  const toast = document.createElement("div");
  toast.className = actionToastClassName(variant);
  if (variant === ToastVariant.Error) {
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
    dismissToast();
  });

  toast.append(text, dismiss);
  document.body.append(toast);
  activeToast = toast;

  const timeout =
    durationMs
    ?? (variant === ToastVariant.Success ? SUCCESS_TOAST_MS : DEFAULT_TOAST_MS);
  activeToastTimer = window.setTimeout(() => {
    dismissToast();
  }, timeout);
}

function dismissToast(): void {
  if (activeToastTimer !== null) {
    window.clearTimeout(activeToastTimer);
    activeToastTimer = null;
  }
  if (activeToast !== null) {
    activeToast.remove();
    activeToast = null;
  }
}
