// Kasa/Tapo KLAP account credentials for the Settings hub.

import { api, HttpError } from "./api.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showSuccessToast } from "./ui-toast.js";
import type { KasaCredentialsSettingsOut } from "./types.js";

function appendKasaIntro(parent: HTMLElement): void {
  const intro = document.createElement("p");
  intro.className = "settings-dialog-lead";
  intro.textContent =
    "Newer Kasa/Tapo devices use KLAP encryption and need your TP-Link account email and password for LAN control. Credentials are stored encrypted in the discovery database on this server.";
  parent.append(intro);
}

export async function mountKasaSettingsPanel(
  container: HTMLElement,
  options: {
    onDevicesChanged?: (() => void | Promise<void>) | undefined;
  },
): Promise<void> {
  container.replaceChildren();

  const form = document.createElement("form");
  form.className = "kasa-settings-form";
  form.noValidate = true;

  appendKasaIntro(form);

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  const emailLabel = document.createElement("label");
  emailLabel.className = "settings-dialog-field";
  const emailText = document.createElement("span");
  emailText.textContent = "Account email";
  const emailInput = document.createElement("input");
  emailInput.type = "email";
  emailInput.name = "username";
  emailInput.autocomplete = "username";
  emailInput.required = true;
  emailInput.placeholder = "you@example.com";
  emailLabel.append(emailText, emailInput);

  const passwordLabel = document.createElement("label");
  passwordLabel.className = "settings-dialog-field";
  const passwordText = document.createElement("span");
  passwordText.textContent = "Password";
  const passwordRow = createSecretInputRow({
    autocomplete: "current-password",
    required: true,
  });
  const passwordInput = passwordRow.input;
  passwordInput.name = "password";
  passwordInput.placeholder = "Account password";
  let passwordStored = false;
  passwordLabel.append(passwordText, passwordRow.row);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  const clearBtn = document.createElement("button");
  clearBtn.type = "button";
  clearBtn.className = "btn btn-secondary";
  clearBtn.textContent = "Clear stored credentials";
  actions.append(saveBtn, clearBtn);
  form.append(status, emailLabel, passwordLabel, actions);
  container.append(form);

  const showStatusMessage = (message: string): void => {
    status.textContent = message;
    status.hidden = false;
  };

  const hideStatus = (): void => {
    status.textContent = "";
    status.hidden = true;
  };

  const applyFieldsFromSettings = (s: KasaCredentialsSettingsOut): void => {
    passwordStored = s.password_stored;
    if (s.stored_username) {
      emailInput.value = s.stored_username;
    }
    passwordInput.value = "";
    // Password is never returned from the API, so every save must include it.
    passwordInput.required = true;
    passwordInput.placeholder = passwordStored
      ? "Re-enter password to update"
      : "Account password";
  };

  const updateStatusHint = (s: KasaCredentialsSettingsOut): void => {
    if (!s.secrets_key_configured) {
      showStatusMessage(
        "Add domesti_secrets_key to domesti-bot.config.json (see domesti-bot.config.json.example) or set DOMESTI_BOT_SECRETS_KEY before saving to the database.",
      );
      return;
    }
    if (s.skipped_auth_hosts.length > 0 && s.configured) {
      const sample = s.skipped_auth_hosts.slice(0, 3).join(", ");
      const more =
        s.skipped_auth_hosts.length > 3
          ? `, … (+${String(s.skipped_auth_hosts.length - 3)} more)`
          : "";
      const envNote =
        s.source === "env"
          ? " KASA_USERNAME / KASA_PASSWORD override the database."
          : "";
      showStatusMessage(
        `${String(s.skipped_auth_hosts.length)} device(s) failed KLAP auth on the last discovery (${sample}${more}). Check the account email/password.${envNote}`,
      );
      return;
    }
    if (s.source === "env") {
      showStatusMessage(
        "KASA_USERNAME / KASA_PASSWORD override the database until you remove them.",
      );
      return;
    }
    if (s.hosts_requiring_klap_auth.length > 0) {
      const sample = s.hosts_requiring_klap_auth.slice(0, 3).join(", ");
      const more =
        s.hosts_requiring_klap_auth.length > 3
          ? `, … (+${String(s.hosts_requiring_klap_auth.length - 3)} more)`
          : "";
      if (s.configured) {
        showStatusMessage(
          `${String(s.hosts_requiring_klap_auth.length)} device(s) use KLAP account auth (${sample}${more}); other Kasa devices stay anonymous on the LAN.`,
        );
      } else {
        showStatusMessage(
          `${String(s.hosts_requiring_klap_auth.length)} device(s) need account credentials and are ignored until you save them (${sample}${more}).`,
        );
      }
      return;
    }
    hideStatus();
  };

  const refreshStatus = async (): Promise<void> => {
    try {
      const s = await api.fetchKasaCredentialsSettings();
      applyFieldsFromSettings(s);
      updateStatusHint(s);
    } catch (err) {
      showStatusMessage(
        err instanceof HttpError ? err.detail : "Could not load credential status.",
      );
    }
  };

  const saveCredentials = (): void => {
    void (async () => {
      const username = emailInput.value.trim();
      const password = passwordInput.value;
      if (!username) {
        showStatusMessage("Enter the Kasa/Tapo account email before saving.");
        return;
      }
      if (!password) {
        showStatusMessage(
          passwordStored
            ? "Re-enter the password to update stored credentials (password is never returned)."
            : "Enter the account password before saving.",
        );
        return;
      }
      saveBtn.disabled = true;
      try {
        const out = await api.putKasaCredentials(username, password);
        showSuccessToast("Kasa credentials saved.");
        passwordRow.setRevealed(false);
        passwordInput.value = "";
        if (out.restart_required) {
          showStatusMessage(
            "Credentials saved. Restart domesti-bot (or remove KASA_USERNAME / KASA_PASSWORD) so devices use them.",
          );
        } else {
          await options.onDevicesChanged?.();
        }
        try {
          const s = await api.fetchKasaCredentialsSettings();
          applyFieldsFromSettings(s);
          if (!out.restart_required) {
            updateStatusHint(s);
          }
        } catch {
          // Save already succeeded; a status-refresh failure is not a save failure.
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
  saveBtn.addEventListener("click", saveCredentials);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveCredentials();
  });

  clearBtn.addEventListener("click", () => {
    void (async () => {
      try {
        await api.clearKasaCredentials();
        emailInput.value = "";
        passwordInput.value = "";
        passwordStored = false;
        passwordInput.required = true;
        passwordInput.placeholder = "Account password";
        passwordRow.setRevealed(false);
        showSuccessToast("Stored Kasa credentials cleared.");
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
