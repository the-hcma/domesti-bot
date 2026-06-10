// Admin credential prompt for My Tracks roster / geofence sync.

import { HttpError } from "./api.js";
import type { RulesDataSource } from "./rules-data-source.js";

export interface MyTracksSyncCredentialDefaults {
  domain: string;
  username: string;
}

export interface MyTracksSyncCredentials {
  password: string;
  username: string;
}

function appendLabeledField(
  parent: HTMLElement,
  labelText: string,
  control: HTMLElement,
): void {
  const field = document.createElement("label");
  field.className = "settings-dialog-field";
  const label = document.createElement("span");
  label.textContent = labelText;
  field.append(label, control);
  parent.append(field);
}

export function promptMyTracksSyncCredentials(
  defaults: MyTracksSyncCredentialDefaults,
): Promise<MyTracksSyncCredentials | null> {
  return new Promise((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "settings-dialog mytracks-sync-dialog";

    const panel = document.createElement("div");
    panel.className = "settings-dialog-panel";

    const header = document.createElement("header");
    header.className = "settings-dialog-header";
    const title = document.createElement("h2");
    title.textContent = "Sync from My Tracks";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "settings-dialog-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "\u00d7";
    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.className = "settings-dialog-body";

    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead";
    const domainHint =
      defaults.domain.trim() === ""
        ? "Configure the My Tracks domain in Settings first."
        : `Connecting to ${defaults.domain.trim()}.`;
    lead.textContent = `${domainHint} Enter an admin username and password to fetch users, devices, and geolocations.`;

    const form = document.createElement("form");
    form.className = "mytracks-sync-form";

    const usernameInput = document.createElement("input");
    usernameInput.type = "text";
    usernameInput.autocomplete = "username";
    usernameInput.required = true;
    usernameInput.value = defaults.username;
    appendLabeledField(form, "Admin username", usernameInput);

    const passwordInput = document.createElement("input");
    passwordInput.type = "password";
    passwordInput.autocomplete = "current-password";
    passwordInput.required = true;
    appendLabeledField(form, "Admin password", passwordInput);

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn btn-secondary";
    cancelBtn.textContent = "Cancel";
    const submitBtn = document.createElement("button");
    submitBtn.type = "submit";
    submitBtn.className = "btn";
    submitBtn.textContent = "Sync";
    actions.append(cancelBtn, submitBtn);
    form.append(actions);

    body.append(lead, form);
    panel.append(header, body);
    dialog.append(panel);

    const finish = (value: MyTracksSyncCredentials | null): void => {
      dialog.close();
      dialog.remove();
      resolve(value);
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
      finish({
        username: usernameInput.value.trim(),
        password: passwordInput.value,
      });
    });

    document.body.append(dialog);
    dialog.showModal();
    usernameInput.focus();
  });
}

export async function runMyTracksSyncAction(
  dataSource: RulesDataSource,
  kind: "geofences" | "participants",
  onComplete: () => void | Promise<void>,
): Promise<void> {
  let settings = null as Awaited<ReturnType<RulesDataSource["getMyTracksSettings"]>>;
  try {
    settings = await dataSource.getMyTracksSettings();
  } catch (err) {
    window.alert(err instanceof Error ? err.message : String(err));
    return;
  }
  const credentials = await promptMyTracksSyncCredentials({
    domain: settings?.domain ?? "",
    username: settings?.username ?? "",
  });
  if (credentials === null) {
    return;
  }
  try {
    if (kind === "participants") {
      await dataSource.syncParticipantsFromMyTracks({
        username: credentials.username,
        password: credentials.password,
      });
    } else {
      await dataSource.syncGeofencesFromMyTracks({
        username: credentials.username,
        password: credentials.password,
      });
    }
    await onComplete();
  } catch (err) {
    const message =
      err instanceof HttpError
        ? err.detail
        : err instanceof Error
          ? err.message
          : String(err);
    window.alert(message);
  }
}
