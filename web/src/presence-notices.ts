// Device presence notices: a device stopped responding (ask what to do) or answers again.
// Rendered above the tile grid; the server owns the notice list (``presence_notices`` in
// ``GET /v1/ui/state``) and the three actions (see ``app/device_presence.py``).

import { PresenceNoticeKind, type UIPresenceNoticeOut } from "./types.js";

export interface PresenceNoticeHandlers {
  onDismiss(notice: UIPresenceNoticeOut): void;
  onForget(notice: UIPresenceNoticeOut): void;
  onKeepChecking(notice: UIPresenceNoticeOut): void;
}

/** Confirmation text shown before a device is forgotten. */
export function presenceForgetConfirmMessage(label: string): string {
  return (
    `Forget ${label}? It will be removed from the dashboard and from the saved device list. ` +
    "Its name and settings are kept if it is added back later."
  );
}

/** Notices whose ids are not in ``seen``, in the order given. */
export function unseenPresenceNotices(
  notices: readonly UIPresenceNoticeOut[],
  seen: ReadonlySet<string>,
): UIPresenceNoticeOut[] {
  return notices.filter((notice) => !seen.has(notice.notice_id));
}

function noticeButton(label: string, onClick: () => void): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "presence-notice-btn";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

/** One banner per notice; ``null`` when there is nothing to show. */
export function createPresenceNoticeStack(
  notices: readonly UIPresenceNoticeOut[],
  handlers: PresenceNoticeHandlers,
): HTMLElement | null {
  if (notices.length === 0) {
    return null;
  }
  const stack = document.createElement("div");
  stack.className = "presence-notices";
  for (const notice of notices) {
    const banner = document.createElement("aside");
    const returned = notice.kind === PresenceNoticeKind.Reappeared;
    banner.className = `presence-notice ${returned ? "presence-notice-returned" : "presence-notice-missing"}`;
    banner.setAttribute("role", returned ? "status" : "alert");
    banner.dataset["noticeId"] = notice.notice_id;

    const text = document.createElement("p");
    text.className = "presence-notice-message";
    text.textContent = notice.message;

    const actions = document.createElement("div");
    actions.className = "presence-notice-actions";
    if (returned) {
      actions.append(noticeButton("Dismiss", () => handlers.onDismiss(notice)));
    } else {
      actions.append(
        noticeButton("Keep checking", () => handlers.onKeepChecking(notice)),
        noticeButton("Forget it", () => handlers.onForget(notice)),
      );
    }
    banner.append(text, actions);
    stack.append(banner);
  }
  return stack;
}
