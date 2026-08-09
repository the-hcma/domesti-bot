// Colored Settings / Automations status lines (success green, failure red).

import { ToastVariant } from "./closed-sets.js";

export function clearSettingsDialogStatus(el: HTMLElement): void {
  el.textContent = "";
  el.hidden = true;
  setSettingsDialogStatusTone(el, null);
}

export function setSettingsDialogStatus(
  el: HTMLElement,
  message: string,
  tone: ToastVariant,
): void {
  el.hidden = false;
  el.textContent = message;
  setSettingsDialogStatusTone(el, tone);
}

export function setSettingsDialogStatusTone(
  el: HTMLElement,
  tone: ToastVariant | null,
): void {
  for (const className of _STATUS_TONE_CLASSES) {
    el.classList.remove(className);
  }
  if (tone === ToastVariant.Error) {
    el.classList.add("settings-dialog-status-error");
  } else if (tone === ToastVariant.Info) {
    el.classList.add("settings-dialog-status-info");
  } else if (tone === ToastVariant.Success) {
    el.classList.add("settings-dialog-status-success");
  }
}

const _STATUS_TONE_CLASSES = [
  "settings-dialog-status-error",
  "settings-dialog-status-info",
  "settings-dialog-status-success",
] as const;
