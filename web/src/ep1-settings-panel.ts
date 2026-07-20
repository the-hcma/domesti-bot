// Everything Presence One Noise pre-shared key (PSK) settings for Settings hub.

import { api, HttpError } from "./api.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showSuccessToast } from "./ui-toast.js";
import type { Ep1NoisePreSharedKeySettingsOut } from "./types.js";

const EP1_DOCS_HREF =
  "https://docs.everythingsmart.io/s/products/doc/everything-presence-one-ep1-3R178yZSUP";

function appendEp1NoisePskIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  const link = document.createElement("a");
  link.href = EP1_DOCS_HREF;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  link.textContent = "EP1 documentation";
  intro.append(
    document.createTextNode(
      "Paste the ESPHome API encryption key — Noise pre-shared key (PSK) — from the ",
    ),
    link,
    document.createTextNode(
      ". It is stored encrypted in the discovery database on this server.",
    ),
  );
  parent.append(intro);
}

export async function mountEp1SettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "ep1-settings-form";
  form.noValidate = true;

  appendEp1NoisePskIntro(form);

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const label = document.createElement("label");
  label.className = "settings-dialog-field";
  const labelText = document.createElement("span");
  labelText.textContent = "Noise pre-shared key (PSK)";
  const secretRow = createSecretInputRow({
    autocomplete: "off",
    required: true,
  });
  const input = secretRow.input;
  input.name = "noise_psk";
  let storedPsk: string | null = null;
  let revealed = false;
  const setRevealed = (next: boolean): void => {
    revealed = next;
    if (next && !input.value && storedPsk) {
      input.value = storedPsk;
    }
    secretRow.setRevealed(next);
  };
  setRevealed(false);
  label.append(labelText, secretRow.row);

  const hostLabel = document.createElement("label");
  hostLabel.className = "settings-dialog-field";
  const hostLabelText = document.createElement("span");
  hostLabelText.textContent = "Test host (optional)";
  const hostInput = document.createElement("input");
  hostInput.type = "text";
  hostInput.name = "host";
  hostInput.placeholder = "192.168.1.50 or host:6053";
  hostInput.autocomplete = "off";
  hostLabel.append(hostLabelText, hostInput);

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
  clearBtn.textContent = "Clear stored key";
  actions.append(saveBtn, testBtn, clearBtn);
  form.append(status, label, hostLabel, actions);
  container.append(form);

  let settingsConfigured = false;

  const syncTestEnabled = (): void => {
    testBtn.disabled = !(input.value.trim() !== "" || settingsConfigured);
  };

  const applyFromSettings = (s: Ep1NoisePreSharedKeySettingsOut): void => {
    settingsConfigured = s.configured;
    storedPsk = s.stored_noise_psk;
    if (storedPsk) {
      input.value = storedPsk;
      input.required = false;
    } else {
      input.value = "";
      input.required = true;
    }
    setRevealed(false);
    status.hidden = true;
    syncTestEnabled();
  };

  const showError = (message: string): void => {
    status.hidden = false;
    status.textContent = message;
    status.classList.add("settings-dialog-status-error");
  };

  try {
    applyFromSettings(await api.fetchEp1NoisePskSettings());
  } catch (err) {
    showError(err instanceof HttpError ? err.detail || err.message : String(err));
  }

  input.addEventListener("input", () => {
    syncTestEnabled();
  });

  saveBtn.addEventListener("click", async () => {
    const noisePsk = input.value.trim();
    if (!noisePsk) {
      showError("Enter a Noise pre-shared key (PSK) before saving.");
      return;
    }
    saveBtn.disabled = true;
    try {
      const out = await api.putEp1NoisePsk(noisePsk);
      applyFromSettings(await api.fetchEp1NoisePskSettings());
      showSuccessToast(
        out.restart_required
          ? "EP1 Noise pre-shared key saved — restart the server to apply."
          : "EP1 Noise pre-shared key saved.",
      );
      await options.onDevicesChanged?.();
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      saveBtn.disabled = false;
    }
  });

  testBtn.addEventListener("click", async () => {
    testBtn.disabled = true;
    try {
      const result = await api.testEp1NoisePsk({
        noise_psk: input.value.trim() || null,
        host: hostInput.value.trim() || null,
      });
      status.hidden = false;
      status.classList.toggle("settings-dialog-status-error", !result.ok);
      status.textContent = result.detail;
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      syncTestEnabled();
    }
  });

  clearBtn.addEventListener("click", async () => {
    clearBtn.disabled = true;
    try {
      await api.clearEp1NoisePsk();
      applyFromSettings(await api.fetchEp1NoisePskSettings());
      showSuccessToast("Stored EP1 Noise pre-shared key cleared.");
      await options.onDevicesChanged?.();
    } catch (err) {
      showError(err instanceof HttpError ? err.detail || err.message : String(err));
    } finally {
      clearBtn.disabled = false;
    }
  });
}
