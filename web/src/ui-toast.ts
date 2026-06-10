// Dismissible toasts and styled confirm prompts (replaces window.alert/confirm).

export type ToastVariant = "error" | "info" | "success";

const DEFAULT_TOAST_MS = 10_000;
const SUCCESS_TOAST_MS = 5_000;

let activeToast: HTMLDivElement | null = null;
let activeToastTimer: number | null = null;

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

export function showToast(
  message: string,
  variant: ToastVariant = "info",
  durationMs?: number,
): void {
  dismissToast();

  const toast = document.createElement("div");
  const variantClass =
    variant === "success"
      ? "action-toast-success"
      : variant === "info"
        ? "action-toast-info"
        : "";
  toast.className =
    variantClass.length > 0 ? `action-toast ${variantClass}` : "action-toast";
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
    dismissToast();
  });

  toast.append(text, dismiss);
  document.body.append(toast);
  activeToast = toast;

  const timeout =
    durationMs
    ?? (variant === "success" ? SUCCESS_TOAST_MS : DEFAULT_TOAST_MS);
  activeToastTimer = window.setTimeout(() => {
    dismissToast();
  }, timeout);
}

export function showErrorToast(message: string): void {
  showToast(message, "error");
}

export function showSuccessToast(message: string): void {
  showToast(message, "success");
}

export function showInfoToast(message: string): void {
  showToast(message, "info");
}

export function confirmAction(options: {
  cancelLabel?: string;
  confirmLabel?: string;
  message: string;
  title?: string;
  variant?: "danger" | "default";
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
      options.variant === "danger" ? "btn btn-danger" : "btn";
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
