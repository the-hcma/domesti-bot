// A small modal asking for the admin API key. Concurrent requests share one prompt.

let inFlight: Promise<string | null> | null = null;

export function promptForKey(message: string): Promise<string | null> {
  if (inFlight) {
    return inFlight;
  }
  inFlight = new Promise<string | null>((resolve) => {
    const dialog = document.createElement("dialog");
    dialog.className = "settings-dialog ui-confirm-dialog admin-key-dialog";
    dialog.setAttribute("aria-describedby", "admin-key-dialog-text");

    const panel = document.createElement("form");
    panel.className = "settings-dialog-panel";
    panel.method = "dialog";

    const text = document.createElement("p");
    text.className = "ui-confirm-body";
    text.id = "admin-key-dialog-text";
    text.textContent = message;

    const field = document.createElement("div");
    field.className = "settings-dialog-field";
    const input = document.createElement("input");
    input.type = "password";
    input.name = "domesti-admin-key";
    input.autocomplete = "off";
    input.setAttribute("aria-label", "API key");
    field.append(input);

    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const cancel = document.createElement("button");
    cancel.type = "button";
    cancel.textContent = "Cancel";
    const submit = document.createElement("button");
    submit.type = "submit";
    submit.textContent = "Continue";
    actions.append(cancel, submit);

    panel.append(text, field, actions);
    dialog.append(panel);
    document.body.append(dialog);

    let answer: string | null = null;
    const finish = (): void => {
      dialog.close();
      dialog.remove();
      inFlight = null;
      resolve(answer);
    };
    cancel.addEventListener("click", finish);
    dialog.addEventListener("cancel", finish);
    panel.addEventListener("submit", (event) => {
      event.preventDefault();
      answer = input.value;
      finish();
    });
    dialog.showModal();
    input.focus();
  });
  return inFlight;
}
