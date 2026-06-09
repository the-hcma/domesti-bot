// My Tracks connection settings (domain + admin credentials).

import { HttpError } from "./api.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel } from "./rules-ui-helpers.js";
import type { MyTracksSettingsIn } from "./types.js";

function appendLabeledField(
  parent: HTMLElement,
  labelEl: HTMLElement,
  control: HTMLElement,
): void {
  const field = document.createElement("label");
  field.className = "settings-dialog-field";
  field.append(labelEl, control);
  parent.append(field);
}

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

export async function mountMyTracksSettingsPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
): Promise<void> {
  container.replaceChildren();

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  let existing = null as Awaited<ReturnType<RulesDataSource["getMyTracksSettings"]>>;
  try {
    existing = await dataSource.getMyTracksSettings();
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not load My Tracks settings: ${formatError(err)}`;
  }

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "My Tracks domain and admin credentials used by Sync from My Tracks on the Participants and Geofences tabs.";

  const form = document.createElement("form");
  form.className = "mytracks-settings-form";
  form.noValidate = true;

  const domainInput = document.createElement("input");
  domainInput.type = "url";
  domainInput.placeholder = "https://tracks.example.com";
  domainInput.required = true;
  domainInput.value = existing?.domain ?? "";
  appendLabeledField(
    form,
    createFieldLabel("My Tracks domain"),
    domainInput,
  );

  const usernameInput = document.createElement("input");
  usernameInput.type = "text";
  usernameInput.autocomplete = "username";
  usernameInput.required = true;
  usernameInput.value = existing?.username ?? "";
  appendLabeledField(
    form,
    createFieldLabel("Admin username"),
    usernameInput,
  );

  const passwordInput = document.createElement("input");
  passwordInput.type = "password";
  passwordInput.autocomplete = "new-password";
  passwordInput.placeholder = existing?.password_configured === true
    ? "Leave blank to keep stored password"
    : "";
  appendLabeledField(
    form,
    createFieldLabel("Admin password"),
    passwordInput,
  );

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "btn btn-secondary";
  resetBtn.textContent = "Clear";
  actions.append(saveBtn, resetBtn);
  form.append(actions);

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    status.hidden = false;
    const payload: MyTracksSettingsIn = {
      domain: domainInput.value.trim(),
      username: usernameInput.value.trim(),
      password: passwordInput.value === "" ? null : passwordInput.value,
    };
    void dataSource
      .saveMyTracksSettings(payload)
      .then((saved) => {
        status.textContent = "My Tracks settings saved.";
        domainInput.value = saved.domain;
        usernameInput.value = saved.username;
        passwordInput.value = "";
        passwordInput.placeholder = saved.password_configured
          ? "Leave blank to keep stored password"
          : "";
      })
      .catch((err: unknown) => {
        status.textContent = formatError(err);
      });
  });

  resetBtn.addEventListener("click", () => {
    if (!window.confirm("Clear stored My Tracks settings?")) {
      return;
    }
    void dataSource
      .resetMyTracksSettings()
      .then(() => {
        domainInput.value = "";
        usernameInput.value = "";
        passwordInput.value = "";
        passwordInput.placeholder = "";
        status.hidden = false;
        status.textContent = "My Tracks settings cleared.";
      })
      .catch((err: unknown) => {
        status.hidden = false;
        status.textContent = formatError(err);
      });
  });

  container.append(lead, status, form);
}
