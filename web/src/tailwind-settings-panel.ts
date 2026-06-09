// GoTailwind token settings panel for the Settings hub.

import { api, HttpError } from "./api.js";
import type { TailwindTokenSettingsOut } from "./types.js";

const TAILWIND_WEB_DASHBOARD_HREF = "https://web.gotailwind.com";

const TOKEN_REVEAL_EYE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

const TOKEN_REVEAL_EYE_OFF_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';

const SETTINGS_TOAST_MS = 5000;

let settingsToast: HTMLDivElement | null = null;
let settingsToastTimer: number | null = null;

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

export async function mountTailwindSettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "tailwind-settings-form";
  form.noValidate = true;

  appendTailwindTokenIntro(form);

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
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored token";
  actions.append(saveBtn, clearBtn);
  form.append(status, label, actions);
  container.append(form);

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
        setTokenRevealed(false);
        const s = await api.fetchTailwindTokenSettings();
        applyTokenFieldsFromSettings(s);
        if (out.restart_required) {
          showStatusMessage(
            "Token saved. Restart domesti-bot (or remove TAILWIND_TOKEN) so garage doors use it.",
          );
        } else {
          updateStatusHint(s);
          await options.onDevicesChanged?.();
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
        await refreshStatus();
        await options.onDevicesChanged?.();
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Clear failed.",
        );
      }
    })();
  });

  await refreshStatus();
}
