// My Tracks domesti-bot pairing and location-history retention settings.

import { api, HttpError } from "./api.js";
import { createFieldLabel, preventBrowserAutofill } from "./rules-ui-helpers.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";
import type {
  LocationHistoryRetentionIn,
  MyTracksPairIn,
  MyTracksPairStatusOut,
} from "./types.js";

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

function defaultDomestiPublicUrl(): string {
  return window.location.origin;
}

function formatError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

function readRetentionFromForm(options: {
  maxAgeHoursInput: HTMLInputElement;
  minKeepCountInput: HTMLInputElement;
  unlimitedInput: HTMLInputElement;
}): LocationHistoryRetentionIn {
  return {
    max_age_hours: Number.parseFloat(options.maxAgeHoursInput.value),
    min_keep_count: Number.parseInt(options.minKeepCountInput.value, 10),
    unlimited: options.unlimitedInput.checked,
  };
}

function setRetentionInputsEnabled(options: {
  enabled: boolean;
  maxAgeHoursInput: HTMLInputElement;
  minKeepCountInput: HTMLInputElement;
}): void {
  options.maxAgeHoursInput.disabled = !options.enabled;
  options.minKeepCountInput.disabled = !options.enabled;
}

function applyRetentionToForm(
  retention: LocationHistoryRetentionIn,
  options: {
    maxAgeHoursInput: HTMLInputElement;
    minKeepCountInput: HTMLInputElement;
    unlimitedInput: HTMLInputElement;
  },
): void {
  options.unlimitedInput.checked = retention.unlimited;
  options.maxAgeHoursInput.value = String(retention.max_age_hours);
  options.minKeepCountInput.value = String(retention.min_keep_count);
  setRetentionInputsEnabled({
    enabled: !retention.unlimited,
    maxAgeHoursInput: options.maxAgeHoursInput,
    minKeepCountInput: options.minKeepCountInput,
  });
}

function renderPairStatus(statusEl: HTMLElement, status: MyTracksPairStatusOut | null): void {
  if (status?.paired_at) {
    statusEl.textContent = `Paired at ${status.paired_at}`;
    return;
  }
  if (status?.last_pair_error) {
    statusEl.textContent = `Last pairing failed: ${status.last_pair_error}`;
    return;
  }
  statusEl.textContent = "Not paired";
}

async function promptPairPassword(username: string): Promise<string | null> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "settings-dialog mytracks-sync-dialog";

    const panel = document.createElement("div");
    panel.className = "settings-dialog-panel";

    const header = document.createElement("header");
    header.className = "settings-dialog-header mytracks-sync-header";
    const title = document.createElement("h2");
    title.textContent = "Pair with My Tracks";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "settings-dialog-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "\u00d7";
    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.className = "settings-dialog-body mytracks-sync-body";
    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead";
    lead.textContent = `Enter the My Tracks admin password for ${username}.`;

    const form = document.createElement("form");
    form.className = "mytracks-sync-form";
    form.setAttribute("autocomplete", "off");
    const passwordRow = createSecretInputRow({
      autocomplete: "new-password",
      required: true,
    });
    appendLabeledField(
      form,
      createFieldLabel("Admin password"),
      passwordRow.row,
    );

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "btn";
    submitBtn.textContent = "Pair";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-secondary";
    cancelBtn.textContent = "Cancel";
    actions.append(submitBtn, cancelBtn);
    form.append(actions);

    body.append(lead, form);
    panel.append(header, body);
    dialog.append(panel);
    document.body.append(dialog);

    const finish = (password: string | null): void => {
      dialog.close();
      dialog.remove();
      resolve(password);
    };

    closeBtn.addEventListener("click", () => {
      finish(null);
    });
    cancelBtn.addEventListener("click", () => {
      finish(null);
    });
    dialog.addEventListener("cancel", (ev) => {
      ev.preventDefault();
      finish(null);
    });
    dialog.addEventListener("click", (ev) => {
      if (ev.target === dialog) {
        finish(null);
      }
    });
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const password = passwordRow.input.value;
      if (password.trim() === "") {
        return;
      }
      finish(password);
    });

    dialog.showModal();
    passwordRow.input.focus();
  });
}

export async function mountMyTracksPairingPanel(container: HTMLElement): Promise<void> {
  const section = document.createElement("section");
  section.className = "mytracks-pairing-section";

  const heading = document.createElement("h3");
  heading.className = "settings-dialog-subheading";
  heading.textContent = "Pairing";

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Register domesti-bot webhook URLs and a relay secret on my-tracks. " +
    "Location history retention applies to live GPS fixes stored on this server.";

  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = false;

  const form = document.createElement("form");
  form.className = "mytracks-pairing-form";
  form.noValidate = true;
  form.setAttribute("autocomplete", "off");

  const fieldsRow = document.createElement("div");
  fieldsRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const domestiInput = document.createElement("input");
  domestiInput.type = "url";
  domestiInput.required = true;
  domestiInput.value = defaultDomestiPublicUrl();
  preventBrowserAutofill(domestiInput);
  appendLabeledField(
    fieldsRow,
    createFieldLabel("domesti-bot public HTTPS URL"),
    domestiInput,
  );
  form.append(fieldsRow);

  const retentionGroup = document.createElement("fieldset");
  retentionGroup.className = "settings-dialog-fieldset";
  const retentionLegend = document.createElement("legend");
  retentionLegend.textContent = "Location history per participant";
  retentionGroup.append(retentionLegend);

  const retentionHelp = document.createElement("p");
  retentionHelp.className = "settings-dialog-help";
  retentionHelp.textContent =
    "Default keeps fixes from the last 24 hours and always keeps at least the 20 most recent fixes.";
  retentionGroup.append(retentionHelp);

  const unlimitedInput = document.createElement("input");
  unlimitedInput.type = "checkbox";
  unlimitedInput.id = "mytracks-location-history-unlimited";
  const unlimitedLabel = document.createElement("label");
  unlimitedLabel.className = "settings-dialog-checkbox";
  unlimitedLabel.htmlFor = unlimitedInput.id;
  unlimitedLabel.append(unlimitedInput, document.createTextNode(" Keep all location history"));
  retentionGroup.append(unlimitedLabel);

  const retentionRow = document.createElement("div");
  retentionRow.className = "settings-dialog-field-row mytracks-settings-fields-row";

  const maxAgeHoursInput = document.createElement("input");
  maxAgeHoursInput.type = "number";
  maxAgeHoursInput.min = "1";
  maxAgeHoursInput.step = "1";
  maxAgeHoursInput.required = true;
  maxAgeHoursInput.value = "24";
  appendLabeledField(
    retentionRow,
    createFieldLabel("Keep fixes from the last (hours)"),
    maxAgeHoursInput,
  );

  const minKeepCountInput = document.createElement("input");
  minKeepCountInput.type = "number";
  minKeepCountInput.min = "1";
  minKeepCountInput.step = "1";
  minKeepCountInput.required = true;
  minKeepCountInput.value = "20";
  appendLabeledField(
    retentionRow,
    createFieldLabel("Minimum recent fixes to keep"),
    minKeepCountInput,
  );
  retentionGroup.append(retentionRow);
  form.append(retentionGroup);

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const pairBtn = document.createElement("button");
  pairBtn.type = "button";
  pairBtn.className = "btn";
  pairBtn.textContent = "Pair";
  const saveRetentionBtn = document.createElement("button");
  saveRetentionBtn.type = "button";
  saveRetentionBtn.className = "btn btn-secondary";
  saveRetentionBtn.textContent = "Save retention";
  actions.append(pairBtn, saveRetentionBtn);
  form.append(actions);

  section.append(heading, lead, status, form);
  container.append(section);

  let pairStatus: MyTracksPairStatusOut | null = null;

  const retentionControls = {
    maxAgeHoursInput,
    minKeepCountInput,
    unlimitedInput,
  };

  unlimitedInput.addEventListener("change", () => {
    setRetentionInputsEnabled({
      enabled: !unlimitedInput.checked,
      maxAgeHoursInput,
      minKeepCountInput,
    });
  });

  async function refreshStatus(): Promise<void> {
    try {
      pairStatus = await api.fetchMyTracksPairStatus();
      if (pairStatus?.domesti_public_base_url) {
        domestiInput.value = pairStatus.domesti_public_base_url;
      }
      if (pairStatus?.location_history_retention) {
        applyRetentionToForm(pairStatus.location_history_retention, retentionControls);
      }
      renderPairStatus(status, pairStatus);
    } catch (err) {
      status.textContent = `Could not load pairing status: ${formatError(err)}`;
    }
  }

  pairBtn.addEventListener("click", () => {
    void (async () => {
      const settings = await api.fetchMyTracksSettings();
      if (settings === null) {
        showErrorToast("Save My Tracks domain and username before pairing.");
        return;
      }
      const password = await promptPairPassword(settings.username);
      if (password === null) {
        return;
      }
      const payload: MyTracksPairIn = {
        domain: settings.domain,
        domesti_public_base_url: domestiInput.value.trim(),
        location_history_retention: readRetentionFromForm(retentionControls),
        password,
        username: settings.username,
      };
      try {
        pairStatus = await api.postMyTracksPair(payload);
        renderPairStatus(status, pairStatus);
        showSuccessToast("My Tracks pairing complete.");
      } catch (err) {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
        await refreshStatus();
      }
    })();
  });

  saveRetentionBtn.addEventListener("click", () => {
    void api
      .patchMyTracksLocationHistoryRetention(readRetentionFromForm(retentionControls))
      .then((saved) => {
        applyRetentionToForm(saved, retentionControls);
        showSuccessToast("Location history retention saved.");
      })
      .catch((err: unknown) => {
        const message = formatError(err);
        status.textContent = message;
        showErrorToast(message);
      });
  });

  await refreshStatus();
}
