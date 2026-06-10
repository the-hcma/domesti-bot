// My Tracks connection settings (domain + default admin username).

import { HttpError } from "./api.js";
import { mountMyTracksPairingPanel } from "./my-tracks-pairing-panel.js";
import { appendMyTracksInstanceText, myTracksHostLabel } from "./mytracks-ui-helpers.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
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
  container.classList.add("mytracks-settings-tab");

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
      " for participant and geofence sync. Pairing saves the domain and admin username; sync prompts for the admin password each time — it is not stored.",
  });

  const connectionSection = document.createElement("div");
  connectionSection.className = "mytracks-connection-section";

  const fieldsRow = document.createElement("div");
  fieldsRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const domainInput = document.createElement("input");
  domainInput.type = "text";
  domainInput.inputMode = "url";
  domainInput.placeholder = "tracks.example.com";
  domainInput.required = true;
  domainInput.value = existing?.domain ? myTracksHostLabel(existing.domain) : "";
  domainInput.spellcheck = false;
  preventBrowserAutofill(domainInput);
  appendLabeledField(
    fieldsRow,
    createFieldLabel("My Tracks domain", {
      detail: "Only HTTPS is supported.",
      example: "mytracks.example.com",
    }),
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
  connectionSection.append(fieldsRow);

  container.append(lead, status, connectionSection);

  const pairingMount = document.createElement("div");
  pairingMount.className = "mytracks-pairing-mount";
  container.append(pairingMount);

  const readConnectionSettings = (): MyTracksSettingsIn => {
    const host = domainInput.value.trim();
    return {
      domain: host === "" ? "" : host,
      username: usernameInput.value.trim(),
    };
  };

  const clearConnectionFields = (): void => {
    domainInput.value = "";
    usernameInput.value = "";
  };

  await mountMyTracksPairingPanel(pairingMount, {
    clearConnectionFields,
    readConnectionSettings,
    resetAllSettings: () => dataSource.resetMyTracksSettings(),
    saveConnectionSettings: (config) => dataSource.saveMyTracksSettings(config),
  });
}
