// My Tracks connection settings (domain + default admin username).

import { HttpError } from "./api.js";
import { appendMyTracksInstanceText } from "./mytracks-ui-helpers.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
import { confirmAction, showErrorToast, showSuccessToast } from "./ui-toast.js";
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
  appendMyTracksInstanceText(lead, {
    before: "Connect to ",
    domain: existing?.domain ?? "",
    after:
      " for participant and geofence sync. Save the domain and default admin username; sync prompts for the admin password each time — it is not stored.",
  });

  const form = document.createElement("form");
  form.className = "mytracks-settings-form";
  form.noValidate = true;
  form.setAttribute("autocomplete", "off");

  const fieldsRow = document.createElement("div");
  fieldsRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const domainInput = document.createElement("input");
  domainInput.type = "url";
  domainInput.placeholder = "https://tracks.example.com";
  domainInput.required = true;
  domainInput.value = existing?.domain ?? "";
  preventBrowserAutofill(domainInput);
  appendLabeledField(
    fieldsRow,
    createFieldLabel("My Tracks domain"),
    domainInput,
  );

  const usernameInput = document.createElement("input");
  usernameInput.type = "text";
  usernameInput.name = "mytracks-default-admin";
  preventBrowserAutofill(usernameInput);
  usernameInput.required = true;
  usernameInput.value = existing?.username ?? "";
  appendLabeledField(
    fieldsRow,
    createFieldLabel("Default admin username"),
    usernameInput,
  );
  form.append(fieldsRow);

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
    };
    void dataSource
      .saveMyTracksSettings(payload)
      .then((saved) => {
        showSuccessToast("My Tracks settings saved.");
        status.textContent = "My Tracks settings saved.";
        domainInput.value = saved.domain;
        usernameInput.value = saved.username;
      })
      .catch((err: unknown) => {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
      });
  });

  resetBtn.addEventListener("click", () => {
    void confirmAction({
      message: "Clear stored My Tracks settings?",
      confirmLabel: "Clear",
      variant: "danger",
    }).then((confirmed) => {
      if (!confirmed) {
        return;
      }
      void dataSource
        .resetMyTracksSettings()
        .then(() => {
          domainInput.value = "";
          usernameInput.value = "";
          status.hidden = false;
          status.textContent = "My Tracks settings cleared.";
          showSuccessToast("My Tracks settings cleared.");
        })
        .catch((err: unknown) => {
          const message = formatError(err);
          status.hidden = false;
          status.textContent = message;
          showErrorToast(message);
        });
    });
  });

  container.append(lead, status, form);
}
