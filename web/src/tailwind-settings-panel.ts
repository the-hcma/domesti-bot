import { ManagedSecretSource, ToastVariant } from "./closed-sets.js";
// GoTailwind token settings panel for the Settings hub.

import { api, HttpError } from "./api.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import {
  clearSettingsDialogStatus,
  setSettingsDialogStatus,
} from "./settings-status.js";
import { showSuccessToast } from "./ui-toast.js";
import type { TailwindHubInfoOut, TailwindTokenSettingsOut } from "./types.js";

const TAILWIND_WEB_DASHBOARD_HREF = "https://web.gotailwind.com";

const HUB_INFO_NOT_CONNECTED =
  "Hub not connected — save a token, then restart domesti-bot so discovery can reach the controller.";

function appendHubInfoRow(list: HTMLElement, term: string, value: string): void {
  const dt = document.createElement("dt");
  dt.textContent = term;
  const dd = document.createElement("dd");
  dd.textContent = value;
  list.append(dt, dd);
}

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

  const hubInfo = document.createElement("dl");
  hubInfo.className = "settings-dialog-info tailwind-hub-info";
  hubInfo.hidden = true;
  const hubInfoEmpty = document.createElement("p");
  hubInfoEmpty.className = "settings-dialog-status";
  hubInfoEmpty.textContent = HUB_INFO_NOT_CONNECTED;
  hubInfoEmpty.hidden = true;

  const renderHubInfo = (info: TailwindHubInfoOut): void => {
    hubInfo.replaceChildren();
    if (!info.reachable) {
      hubInfo.hidden = true;
      hubInfoEmpty.hidden = false;
      return;
    }
    if (info.product) appendHubInfoRow(hubInfo, "Model", info.product);
    if (info.firmware_version) appendHubInfoRow(hubInfo, "Firmware", info.firmware_version);
    if (info.protocol_version) appendHubInfoRow(hubInfo, "Protocol", info.protocol_version);
    if (info.number_of_doors !== null)
      appendHubInfoRow(hubInfo, "Doors", String(info.number_of_doors));
    if (info.hub_mac) appendHubInfoRow(hubInfo, "Hub MAC", info.hub_mac);
    if (info.host) appendHubInfoRow(hubInfo, "Host", info.host);
    hubInfo.hidden = false;
    hubInfoEmpty.hidden = true;
  };

  let hubInfoRefreshGeneration = 0;
  const refreshHubInfo = async (): Promise<void> => {
    const generation = ++hubInfoRefreshGeneration;
    try {
      const info = await api.fetchTailwindHubInfo();
      if (generation !== hubInfoRefreshGeneration) return;
      renderHubInfo(info);
    } catch {
      if (generation !== hubInfoRefreshGeneration) return;
      hubInfo.hidden = true;
      hubInfoEmpty.hidden = true;
    }
  };

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = "Token";
  const tokenRow = createSecretInputRow({
    autocomplete: "off",
    inputMode: "numeric",
    maxLength: 64,
    required: true,
  });
  const input = tokenRow.input;
  input.name = "token";
  let storedToken: string | null = null;
  let tokenRevealed = false;
  const setTokenRevealed = (revealed: boolean): void => {
    tokenRevealed = revealed;
    if (revealed && !input.value && storedToken) {
      input.value = storedToken;
    }
    tokenRow.setRevealed(revealed);
  };
  setTokenRevealed(false);
  label.append(labelText, tokenRow.row);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Test";
  testBtn.disabled = true;
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored token";
  actions.append(saveBtn, testBtn, clearBtn);
  form.append(hubInfo, hubInfoEmpty, status, label, actions);
  container.append(form);

  let settingsConfigured = false;

  const syncTestEnabled = (): void => {
    testBtn.disabled = !(input.value.trim() !== "" || settingsConfigured);
  };

  const applyTokenFieldsFromSettings = (s: TailwindTokenSettingsOut): void => {
    settingsConfigured = s.configured;
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
    syncTestEnabled();
  };

  const showStatusMessage = (
    message: string,
    tone: ToastVariant = ToastVariant.Info,
  ): void => {
    setSettingsDialogStatus(status, message, tone);
  };

  const hideStatus = (): void => {
    clearSettingsDialogStatus(status);
  };

  const updateStatusHint = (s: TailwindTokenSettingsOut): void => {
    if (!s.secrets_key_configured) {
      showStatusMessage(
        "Add domesti_secrets_key to domesti-secrets.json at the repo root (see domesti-secrets.json.example) or set DOMESTI_SECRETS_KEY before saving to the database.",
      );
      return;
    }
    if (s.source === ManagedSecretSource.Env || s.source === ManagedSecretSource.Cli) {
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
        ToastVariant.Error,
      );
    }
  };

  const saveToken = (): void => {
    void (async () => {
      const token = input.value.trim();
      if (!token) {
        showStatusMessage("Enter a token before saving.", ToastVariant.Error);
        return;
      }
      saveBtn.disabled = true;
      try {
        const out = await api.putTailwindToken(token);
        showSuccessToast("Token saved.");
        setTokenRevealed(false);
        const s = await api.fetchTailwindTokenSettings();
        applyTokenFieldsFromSettings(s);
        await refreshHubInfo();
        if (out.restart_required) {
          showStatusMessage(
            "Token saved. Restart domesti-bot (or remove TAILWIND_TOKEN) so garage doors use it.",
            ToastVariant.Success,
          );
        } else {
          updateStatusHint(s);
          await options.onDevicesChanged?.();
        }
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Save failed.",
          ToastVariant.Error,
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
  input.addEventListener("input", () => {
    syncTestEnabled();
  });

  testBtn.addEventListener("click", () => {
    void (async () => {
      const token = input.value.trim();
      testBtn.disabled = true;
      showStatusMessage("Testing token…");
      try {
        const result = await api.testTailwindToken(
          token !== "" ? { token } : {},
        );
        showStatusMessage(
          result.detail,
          result.ok ? ToastVariant.Success : ToastVariant.Error,
        );
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Test failed.",
          ToastVariant.Error,
        );
      } finally {
        syncTestEnabled();
      }
    })();
  });

  clearBtn.addEventListener("click", () => {
    void (async () => {
      try {
        await api.clearTailwindToken();
        storedToken = null;
        settingsConfigured = false;
        input.value = "";
        input.required = true;
        setTokenRevealed(false);
        syncTestEnabled();
        showSuccessToast("Stored token cleared.");
        await refreshStatus();
        await refreshHubInfo();
        await options.onDevicesChanged?.();
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Clear failed.",
          ToastVariant.Error,
        );
      }
    })();
  });

  await refreshStatus();
  await refreshHubInfo();
}
