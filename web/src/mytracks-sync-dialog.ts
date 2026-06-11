// Admin credential prompt for My Tracks roster / geofence sync.

import { HttpError } from "./api.js";
import { appendMyTracksInstanceText } from "./mytracks-ui-helpers.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { preventBrowserAutofill } from "./rules-ui-helpers.js";
import { createSecretInputRow } from "./settings-secret-field.js";
import { showErrorToast, showSuccessToast } from "./ui-toast.js";

export interface MyTracksSyncCredentialDefaults {
  domain: string;
  username: string;
}

export interface MyTracksSyncCredentials {
  password: string;
  username: string;
}

function appendInlineField(
  parent: HTMLElement,
  labelText: string,
  control: HTMLElement,
): void {
  const field = document.createElement("label");
  field.className = "settings-dialog-field mytracks-sync-field";
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
    header.className = "settings-dialog-header mytracks-sync-header";
    const title = document.createElement("h2");
    title.textContent = "Sync from My Tracks";
    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.className = "settings-dialog-close";
    closeBtn.setAttribute("aria-label", "Close");
    closeBtn.textContent = "\u00d7";
    header.append(title, closeBtn);

    const body = document.createElement("div");
    body.className = "settings-dialog-body mytracks-sync-body";

    const lead = document.createElement("p");
    lead.className = "settings-dialog-lead mytracks-sync-lead";
    const domain = defaults.domain.trim();
    if (domain === "") {
      lead.textContent =
        "Configure the My Tracks domain in Settings first, then enter admin credentials.";
    } else {
      appendMyTracksInstanceText(lead, {
        before: "Sign in to ",
        domain,
        after: " to fetch users, devices, and latest locations.",
      });
    }

    const form = document.createElement("form");
    form.className = "mytracks-sync-form";
    form.setAttribute("autocomplete", "off");

    const credentialsRow = document.createElement("div");
    credentialsRow.className = "settings-dialog-field-row mytracks-sync-credentials-row";

    const usernameInput = document.createElement("input");
    usernameInput.type = "text";
    usernameInput.name = "mytracks-sync-username";
    usernameInput.required = true;
    usernameInput.value = defaults.username;
    preventBrowserAutofill(usernameInput);
    appendInlineField(credentialsRow, "Admin username", usernameInput);

    const passwordRow = createSecretInputRow({
      required: true,
    });
    passwordRow.input.name = "mytracks-sync-password";
    passwordRow.input.setAttribute("autocomplete", "new-password");
    passwordRow.input.required = true;
    appendInlineField(credentialsRow, "Admin password", passwordRow.row);
    form.append(credentialsRow);

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions mytracks-sync-actions";
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
        password: passwordRow.input.value,
      });
    });

    document.body.append(dialog);
    dialog.showModal();
    usernameInput.focus();
  });
}

export async function runMyTracksSyncAction(
  dataSource: RulesDataSource,
  kind: "geofences" | "users",
  onComplete: () => void | Promise<void>,
): Promise<void> {
  let settings = null as Awaited<ReturnType<RulesDataSource["getMyTracksSettings"]>>;
  try {
    settings = await dataSource.getMyTracksSettings();
  } catch (err) {
    showErrorToast(err instanceof Error ? err.message : String(err));
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
    if (kind === "users") {
      const result = await dataSource.syncParticipantsFromMyTracks({
        username: credentials.username,
        password: credentials.password,
      });
      showSuccessToast(
        `Synced ${result.user_count} participant${result.user_count === 1 ? "" : "s"} from My Tracks.`,
      );
    } else {
      const result = await dataSource.syncGeofencesFromMyTracks({
        username: credentials.username,
        password: credentials.password,
      });
      showSuccessToast(
        `Synced ${result.geofence_count} geofence${result.geofence_count === 1 ? "" : "s"} from My Tracks.`,
      );
    }
    await onComplete();
  } catch (err) {
    const message =
      err instanceof HttpError
        ? err.detail
        : err instanceof Error
          ? err.message
          : String(err);
    showErrorToast(message);
  }
}
