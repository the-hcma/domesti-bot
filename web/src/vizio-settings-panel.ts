// Vizio SmartCast TV settings panel (per-TV auth + optional PIN pairing).

import { api, HttpError } from "./api.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showSuccessToast } from "./ui-toast.js";
import type { VizioPairBeginOut, VizioTvSettingsOut } from "./types.js";

const DEFAULT_VIZIO_PORT = 7345;

function vizioDeviceIdFromHostInput(raw: string): string {
  const text = raw.trim();
  const colon = text.lastIndexOf(":");
  if (colon === -1) {
    return text;
  }
  const host = text.slice(0, colon).trim();
  const port = Number(text.slice(colon + 1));
  if (!host || Number.isNaN(port)) {
    return text;
  }
  return port === DEFAULT_VIZIO_PORT ? host : `${host}:${port}`;
}

function appendVizioIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Pair with a PIN shown on the TV, or paste an auth token from a prior " +
    "SmartCast pairing session. Enter the TV's current IP to reach it; " +
    "domesti-bot stores the MAC as the stable device id so DHCP changes do " +
    "not break control.";
  parent.append(intro);
}

const DEFAULT_TV_HOST = "192.168.86.201";

function findTvRow(
  tvs: readonly VizioTvSettingsOut[],
  host: string,
): VizioTvSettingsOut | undefined {
  const hostDeviceId = vizioDeviceIdFromHostInput(host);
  const hostOnly = host.trim();
  return tvs.find(
    (tv) =>
      tv.device_id === hostDeviceId ||
      tv.host === hostOnly ||
      tv.host === hostDeviceId,
  );
}

export async function mountVizioSettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "vizio-settings-form";
  form.noValidate = true;

  appendVizioIntro(form);

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const hostLabel = document.createElement("label");
  hostLabel.className = "settings-dialog-field";
  const hostLabelText = document.createElement("span");
  hostLabelText.textContent = "TV host";
  const hostInput = document.createElement("input");
  hostInput.type = "text";
  hostInput.name = "host";
  hostInput.required = true;
  hostInput.placeholder = "192.168.86.201 or 192.168.86.201:7345";
  hostInput.value = DEFAULT_TV_HOST;
  hostInput.autocomplete = "off";
  hostLabel.append(hostLabelText, hostInput);

  const macLabel = document.createElement("label");
  macLabel.className = "settings-dialog-field";
  macLabel.hidden = true;
  const macLabelText = document.createElement("span");
  macLabelText.textContent = "MAC address";
  const macInput = document.createElement("input");
  macInput.type = "text";
  macInput.name = "mac";
  macInput.readOnly = true;
  macInput.autocomplete = "off";
  macInput.placeholder = "Resolved after pairing or saving a token";
  macLabel.append(macLabelText, macInput);

  const tokenLabel = document.createElement("label");
  tokenLabel.className = "settings-dialog-field";
  const tokenLabelText = document.createElement("span");
  tokenLabelText.textContent = "Auth token";
  const tokenRow = createSecretInputRow({
    autocomplete: "off",
    maxLength: 256,
    placeholder: "Paste token from pairing",
    required: false,
  });
  const tokenInput = tokenRow.input;
  tokenInput.name = "token";
  let storedToken: string | null = null;
  let tokenRevealed = false;
  const setTokenRevealed = (revealed: boolean): void => {
    tokenRevealed = revealed;
    if (revealed && !tokenInput.value && storedToken) {
      tokenInput.value = storedToken;
    }
    tokenRow.setRevealed(revealed);
  };
  setTokenRevealed(false);
  tokenLabel.append(tokenLabelText, tokenRow.row);

  const pairSection = document.createElement("fieldset");
  pairSection.className = "settings-dialog-fieldset vizio-pairing-section";
  const pairLegend = document.createElement("legend");
  pairLegend.textContent = "Pair with PIN";
  pairSection.append(pairLegend);

  const pairStatus = document.createElement("p");
  pairStatus.className = "settings-dialog-status";
  pairStatus.hidden = true;
  pairSection.append(pairStatus);

  const pinLabel = document.createElement("label");
  pinLabel.className = "settings-dialog-field";
  pinLabel.hidden = true;
  const pinLabelText = document.createElement("span");
  pinLabelText.textContent = "PIN on TV";
  const pinInput = document.createElement("input");
  pinInput.type = "text";
  pinInput.inputMode = "numeric";
  pinInput.pattern = "[0-9]*";
  pinInput.maxLength = 8;
  pinInput.autocomplete = "one-time-code";
  pinInput.placeholder = "4-digit PIN";
  pinLabel.append(pinLabelText, pinInput);
  pairSection.append(pinLabel);

  const pairActions = document.createElement("div");
  pairActions.className = "settings-dialog-actions";
  const beginPairBtn = document.createElement("button");
  beginPairBtn.type = "button";
  beginPairBtn.className = "btn btn-secondary";
  beginPairBtn.textContent = "Start pairing";
  const completePairBtn = document.createElement("button");
  completePairBtn.type = "button";
  completePairBtn.className = "btn";
  completePairBtn.textContent = "Complete pairing";
  completePairBtn.hidden = true;
  const cancelPairBtn = document.createElement("button");
  cancelPairBtn.type = "button";
  cancelPairBtn.className = "btn btn-secondary";
  cancelPairBtn.textContent = "Cancel pairing";
  cancelPairBtn.hidden = true;
  pairActions.append(beginPairBtn, completePairBtn, cancelPairBtn);
  pairSection.append(pairActions);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save token";
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Test";
  testBtn.disabled = true;
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored token";
  clearBtn.disabled = true;
  actions.append(saveBtn, testBtn, clearBtn);

  form.append(status, hostLabel, macLabel, tokenLabel, pairSection, actions);
  container.append(form);

  let pendingPair: VizioPairBeginOut | null = null;
  let paired = false;
  let authConfigured = false;

  const syncTestEnabled = (): void => {
    testBtn.disabled = !(
      tokenInput.value.trim() !== "" || authConfigured
    );
  };

  const showStatusMessage = (message: string): void => {
    status.textContent = message;
    status.hidden = false;
  };

  const hideStatus = (): void => {
    status.textContent = "";
    status.hidden = true;
  };

  const showPairStatus = (message: string): void => {
    pairStatus.textContent = message;
    pairStatus.hidden = false;
  };

  const hidePairStatus = (): void => {
    pairStatus.textContent = "";
    pairStatus.hidden = true;
  };

  const applyTokenFieldsFromTv = (tv: VizioTvSettingsOut | undefined): void => {
    storedToken = tv?.stored_token ?? null;
    paired = tv?.auth_configured === true;
    authConfigured = tv?.auth_configured === true;
    if (storedToken) {
      tokenInput.value = storedToken;
      tokenInput.placeholder = "";
      if (!tokenRevealed) {
        tokenInput.type = "password";
      }
    } else {
      tokenInput.value = "";
      tokenInput.placeholder = paired ? "" : "Paste token from pairing";
      if (!tokenRevealed) {
        tokenInput.type = "password";
      }
    }
    tokenInput.required = !paired;
    saveBtn.hidden = paired;
    beginPairBtn.textContent = paired ? "Re-pair" : "Start pairing";
    clearBtn.disabled = !paired || tv?.auth_source !== "database";
    syncTestEnabled();
  };

  const setPairingUiActive = (active: boolean): void => {
    beginPairBtn.hidden = active;
    completePairBtn.hidden = !active;
    cancelPairBtn.hidden = !active;
    pinLabel.hidden = !active;
    if (!active) {
      pinInput.value = "";
      pendingPair = null;
      hidePairStatus();
      beginPairBtn.textContent = paired ? "Re-pair" : "Start pairing";
    }
  };

  const applyMacField = (tv: VizioTvSettingsOut | undefined): void => {
    const mac = tv?.mac ?? null;
    if (mac) {
      macInput.value = mac;
      macLabel.hidden = false;
      return;
    }
    macInput.value = "";
    macLabel.hidden = true;
  };

  const applyTvStatus = (tv: VizioTvSettingsOut | undefined): void => {
    applyTokenFieldsFromTv(tv);
    applyMacField(tv);
    if (tv?.auth_configured) {
      const name = tv.display_name ?? tv.host;
      const label = tv.mac ? `${name} (${tv.mac})` : name;
      if (tv.auth_source === "cli") {
        showStatusMessage(
          `${label} uses --vizio-auth-token; per-TV database tokens are ignored until you remove the CLI flag.`,
        );
        return;
      }
      if (tv.auth_source === "env") {
        showStatusMessage(
          `${label} uses VIZIO_AUTH_TOKEN; no per-TV database token is configured for this host.`,
        );
        return;
      }
      showStatusMessage(`${label} is paired (stored in database).`);
      return;
    }
    hideStatus();
  };

  const refreshStatus = async (): Promise<void> => {
    try {
      const settings = await api.fetchVizioTvsSettings();
      if (!settings.secrets_key_configured) {
        showStatusMessage(
          "Add domesti_secrets_key to domesti-bot.config.json at the repo root (see domesti-bot.config.json.example) or set DOMESTI_BOT_SECRETS_KEY before saving to the database.",
        );
        clearBtn.disabled = true;
        saveBtn.hidden = false;
        return;
      }
      const tv = findTvRow(settings.tvs, hostInput.value);
      applyTvStatus(tv);
    } catch (err) {
      showStatusMessage(
        err instanceof HttpError ? err.detail : "Could not load Vizio TV status.",
      );
    }
  };

  hostInput.addEventListener("change", () => {
    void refreshStatus();
  });

  const saveToken = (): void => {
    void (async () => {
      const host = hostInput.value.trim();
      const token = tokenInput.value.trim();
      if (!host) {
        showStatusMessage("Enter the TV host before saving.");
        return;
      }
      if (!token) {
        showStatusMessage("Enter an auth token before saving.");
        return;
      }
      saveBtn.disabled = true;
      try {
        const out = await api.putVizioAuthToken(host, token);
        showSuccessToast("Vizio TV token saved.");
        setTokenRevealed(false);
        if (out.restart_required) {
          showStatusMessage(
            "Token saved. Restart domesti-bot so the TV tile appears.",
          );
        } else {
          await refreshStatus();
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
    if (!saveBtn.hidden) {
      saveToken();
    }
  });
  tokenInput.addEventListener("input", () => {
    syncTestEnabled();
  });

  testBtn.addEventListener("click", () => {
    void (async () => {
      const host = hostInput.value.trim();
      if (!host) {
        showStatusMessage("Enter the TV host before testing.");
        return;
      }
      const token = tokenInput.value.trim();
      let deviceId = vizioDeviceIdFromHostInput(host);
      try {
        const settings = await api.fetchVizioTvsSettings();
        const tv = findTvRow(settings.tvs, host);
        if (tv !== undefined) {
          deviceId = tv.device_id;
        }
      } catch {
        // Fall back to host-based id when status cannot be loaded.
      }
      testBtn.disabled = true;
      showStatusMessage("Testing auth…");
      try {
        const result = await api.testVizioAuth(
          deviceId,
          token !== "" ? { token } : {},
        );
        showStatusMessage(result.detail);
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Test failed.",
        );
      } finally {
        syncTestEnabled();
      }
    })();
  });

  beginPairBtn.addEventListener("click", () => {
    void (async () => {
      const host = hostInput.value.trim();
      if (!host) {
        showPairStatus("Enter the TV host before starting pairing.");
        return;
      }
      beginPairBtn.disabled = true;
      try {
        pendingPair = await api.beginVizioPairing(host);
        setPairingUiActive(true);
        showPairStatus(
          "Enter the PIN shown on the TV, then click Complete pairing.",
        );
      } catch (err) {
        showPairStatus(
          err instanceof HttpError ? err.detail : "Could not start pairing.",
        );
      } finally {
        beginPairBtn.disabled = false;
      }
    })();
  });

  completePairBtn.addEventListener("click", () => {
    void (async () => {
      if (pendingPair === null) {
        showPairStatus("Start pairing first.");
        return;
      }
      const pin = pinInput.value.trim();
      if (!pin) {
        showPairStatus("Enter the PIN from the TV.");
        return;
      }
      completePairBtn.disabled = true;
      try {
        const out = await api.completeVizioPairing({
          device_id: pendingPair.device_id,
          pin,
          challenge_type: pendingPair.challenge_type,
          pairing_req_token: pendingPair.pairing_req_token,
        });
        showSuccessToast("Vizio TV paired.");
        setPairingUiActive(false);
        setTokenRevealed(false);
        if (out.restart_required) {
          showStatusMessage(
            "Pairing complete. Restart domesti-bot so the TV tile appears.",
          );
        } else {
          await refreshStatus();
          await options.onDevicesChanged?.();
        }
      } catch (err) {
        showPairStatus(
          err instanceof HttpError ? err.detail : "Pairing failed.",
        );
      } finally {
        completePairBtn.disabled = false;
      }
    })();
  });

  cancelPairBtn.addEventListener("click", () => {
    void (async () => {
      if (pendingPair === null) {
        setPairingUiActive(false);
        return;
      }
      cancelPairBtn.disabled = true;
      try {
        await api.cancelVizioPairing({
          device_id: pendingPair.device_id,
          challenge_type: pendingPair.challenge_type,
          pairing_req_token: pendingPair.pairing_req_token,
        });
        setPairingUiActive(false);
        showPairStatus("Pairing cancelled.");
      } catch (err) {
        showPairStatus(
          err instanceof HttpError ? err.detail : "Cancel failed.",
        );
      } finally {
        cancelPairBtn.disabled = false;
      }
    })();
  });

  clearBtn.addEventListener("click", () => {
    void (async () => {
      const host = hostInput.value.trim();
      if (!host) {
        showStatusMessage("Enter the TV host before clearing.");
        return;
      }
      let deviceId = vizioDeviceIdFromHostInput(host);
      try {
        const settings = await api.fetchVizioTvsSettings();
        const tv = findTvRow(settings.tvs, host);
        if (tv !== undefined) {
          deviceId = tv.device_id;
        }
      } catch {
        // Fall back to host-based id when status cannot be loaded.
      }
      clearBtn.disabled = true;
      try {
        await api.clearVizioAuth(deviceId);
        storedToken = null;
        tokenInput.value = "";
        setTokenRevealed(false);
        showSuccessToast("Stored token cleared.");
        await refreshStatus();
        await options.onDevicesChanged?.();
      } catch (err) {
        showStatusMessage(
          err instanceof HttpError ? err.detail : "Clear failed.",
        );
      } finally {
        clearBtn.disabled = false;
      }
    })();
  });

  setPairingUiActive(false);
  await refreshStatus();
}
