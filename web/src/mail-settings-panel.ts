// SMTP / mail settings panel (persisted via ``/v1/settings/smtp`` when available).

import { HttpError } from "./api.js";
import type { RulesDataSource } from "./rules-data-source.js";
import { createFieldLabel } from "./rules-ui-helpers.js";
import type { SmtpConfigIn, SmtpConfigOut } from "./types.js";

const DEFAULT_SMTP_HOST = "localhost";
const DEFAULT_SMTP_PORT = 25;
const DEFAULT_FROM_LOCALPART = "domestibot-noreply";

const SMTP_PORT_OPTIONS: readonly { label: string; port: number }[] = [
  { port: 25, label: "25 — SMTP (plain)" },
  { port: 465, label: "465 — SMTPS (implicit SSL)" },
  { port: 587, label: "587 — Submission (STARTTLS)" },
  { port: 2525, label: "2525 — Alternative (STARTTLS)" },
];

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

function defaultFromAddress(domain: string): string {
  const trimmed = domain.trim();
  if (trimmed === "") {
    return "";
  }
  return `${DEFAULT_FROM_LOCALPART}@${trimmed}`;
}

function formatMailError(err: unknown): string {
  if (err instanceof HttpError) {
    return err.detail;
  }
  return err instanceof Error ? err.message : "Unexpected error";
}

function configToForm(config: SmtpConfigOut | null): {
  from_address: string;
  host: string;
  mail_domain: string;
  port: number;
  username: string;
} {
  return {
    host: config?.host ?? DEFAULT_SMTP_HOST,
    port: config?.port ?? DEFAULT_SMTP_PORT,
    username: config?.username ?? "",
    mail_domain: config?.mail_domain ?? "",
    from_address: config?.from_address ?? "",
  };
}

export async function mountMailSettingsPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
): Promise<void> {
  container.replaceChildren();
  const status = document.createElement("p");
  status.className = "settings-dialog-status";
  status.hidden = true;

  let existing: SmtpConfigOut | null = null;
  try {
    existing = await dataSource.getSmtpConfig();
  } catch (err) {
    status.hidden = false;
    status.textContent = `Could not load SMTP settings: ${formatMailError(err)}`;
  }
  const defaults = configToForm(existing);
  let testPassed = existing !== null;
  let fromAddressManual = existing !== null && defaults.from_address !== "";

  const lead = document.createElement("p");
  lead.className = "settings-dialog-lead";
  lead.textContent =
    "Outgoing email for rule notifications. Send a successful test before saving.";

  const form = document.createElement("form");
  form.className = "rules-mail-form";
  form.noValidate = true;

  const hostInput = document.createElement("input");
  hostInput.type = "text";
  hostInput.placeholder = DEFAULT_SMTP_HOST;
  hostInput.value = defaults.host;
  hostInput.required = true;
  appendLabeledField(form, createFieldLabel("SMTP host"), hostInput);

  const portSelect = document.createElement("select");
  for (const opt of SMTP_PORT_OPTIONS) {
    const el = document.createElement("option");
    el.value = String(opt.port);
    el.textContent = opt.label;
    portSelect.append(el);
  }
  portSelect.value = String(defaults.port);
  appendLabeledField(form, createFieldLabel("Port"), portSelect);

  const domainInput = document.createElement("input");
  domainInput.type = "text";
  domainInput.placeholder = "hcma.info";
  domainInput.value = defaults.mail_domain;
  domainInput.required = true;
  appendLabeledField(
    form,
    createFieldLabel("Mail domain", {
      detail:
        "Domain used for the default From address when sending rule notifications.",
      example: "hcma.info → domestibot-noreply@hcma.info",
    }),
    domainInput,
  );

  const usernameInput = document.createElement("input");
  usernameInput.type = "text";
  usernameInput.autocomplete = "off";
  usernameInput.placeholder = "leave blank if not required";
  usernameInput.value = defaults.username;
  appendLabeledField(
    form,
    createFieldLabel("Username (optional)"),
    usernameInput,
  );

  const passwordInput = document.createElement("input");
  passwordInput.type = "password";
  passwordInput.autocomplete = "new-password";
  passwordInput.placeholder = existing?.password_configured
    ? "leave blank to keep current"
    : "leave blank if not required";
  appendLabeledField(
    form,
    createFieldLabel("Password (optional)"),
    passwordInput,
  );

  const fromInput = document.createElement("input");
  fromInput.type = "email";
  fromInput.placeholder = `${DEFAULT_FROM_LOCALPART}@example.com`;
  fromInput.value =
    defaults.from_address !== ""
      ? defaults.from_address
      : defaultFromAddress(defaults.mail_domain);
  appendLabeledField(
    form,
    createFieldLabel("From address"),
    fromInput,
  );

  const saveSmtpSettings = (): void => {
    if (!testPassed) {
      return;
    }
    void dataSource
      .saveSmtpConfig(readDraft())
      .then((saved) => {
        status.hidden = false;
        status.textContent = `Saved SMTP settings for ${saved.host}:${saved.port}`;
        passwordInput.value = "";
        passwordInput.placeholder = "leave blank to keep current";
        resetBtn.disabled = false;
        fromAddressManual = true;
      })
      .catch((err: unknown) => {
        status.hidden = false;
        status.textContent = formatMailError(err);
      });
  };

  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const saveBtn = document.createElement("button");
  saveBtn.type = "button";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save SMTP settings";
  saveBtn.addEventListener("click", saveSmtpSettings);
  const resetBtn = document.createElement("button");
  resetBtn.type = "button";
  resetBtn.className = "btn btn-danger";
  resetBtn.textContent = "Reset SMTP settings";
  resetBtn.disabled = existing === null;
  actions.append(saveBtn, resetBtn);

  const syncSaveEnabled = (): void => {
    saveBtn.disabled = !testPassed;
    saveBtn.title = testPassed
      ? ""
      : "Send a successful test email first";
  };
  syncSaveEnabled();

  const readDraft = (): SmtpConfigIn => ({
    host: hostInput.value.trim(),
    port: Number(portSelect.value) || DEFAULT_SMTP_PORT,
    username: usernameInput.value.trim(),
    password: passwordInput.value === "" ? null : passwordInput.value,
    mail_domain: domainInput.value.trim(),
    from_address: fromInput.value.trim(),
  });

  const markDirty = (): void => {
    testPassed = false;
    syncSaveEnabled();
  };
  for (const el of [hostInput, portSelect, usernameInput, passwordInput]) {
    el.addEventListener("input", markDirty);
    el.addEventListener("change", markDirty);
  }

  domainInput.addEventListener("input", () => {
    markDirty();
    if (!fromAddressManual) {
      fromInput.value = defaultFromAddress(domainInput.value);
    }
  });
  fromInput.addEventListener("input", () => {
    markDirty();
    fromAddressManual = true;
  });

  const testHeading = document.createElement("h3");
  testHeading.className = "rules-section-title";
  testHeading.textContent = "Send test email";
  const testLead = document.createElement("p");
  testLead.className = "settings-dialog-lead";
  testLead.textContent =
    "Verify connectivity using the settings above — you can test before saving.";
  const testRow = document.createElement("div");
  testRow.className = "rules-mail-test-row";
  const testTo = document.createElement("input");
  testTo.type = "email";
  testTo.placeholder = "recipient@example.com";
  testTo.value = existing?.last_test_recipient ?? "";
  testTo.required = true;
  const testBtn = document.createElement("button");
  testBtn.type = "button";
  testBtn.className = "btn btn-secondary";
  testBtn.textContent = "Send test email";
  testRow.append(testTo, testBtn);

  testBtn.addEventListener("click", () => {
    status.hidden = false;
    if (hostInput.value.trim() === "") {
      status.textContent = "Expected SMTP host, got empty value";
      return;
    }
    if (domainInput.value.trim() === "") {
      status.textContent = "Expected mail domain, got empty value";
      return;
    }
    if (testTo.value.trim() === "") {
      status.textContent = "Expected recipient email, got empty value";
      return;
    }
    testBtn.disabled = true;
    status.textContent = "Sending test email…";
    void dataSource
      .sendSmtpTestEmail({
        ...readDraft(),
        to_address: testTo.value.trim(),
      })
      .then((result) => {
        status.textContent = result.message;
        if (result.ok) {
          testPassed = true;
          syncSaveEnabled();
        }
      })
      .catch((err: unknown) => {
        status.textContent = formatMailError(err);
      })
      .finally(() => {
        testBtn.disabled = false;
      });
  });

  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    saveSmtpSettings();
  });

  resetBtn.addEventListener("click", () => {
    if (
      !window.confirm(
        "This will permanently delete all SMTP settings, including the stored password. Continue?",
      )
    ) {
      return;
    }
    void dataSource
      .resetSmtpConfig()
      .then(() => {
        void mountMailSettingsPanel(container, dataSource);
      })
      .catch((err: unknown) => {
        status.hidden = false;
        status.textContent = formatMailError(err);
      });
  });

  form.append(actions);
  container.append(lead, form, status, testHeading, testLead, testRow);
}
