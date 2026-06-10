// Password / token row with reveal toggle (Settings hub).

export const SECRET_REVEAL_EYE_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';

export const SECRET_REVEAL_EYE_OFF_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><path d="M1 1l22 22"/></svg>';

export interface SecretInputRow {
  input: HTMLInputElement;
  row: HTMLDivElement;
  setRevealed: (revealed: boolean) => void;
}

export function createSecretInputRow(options: {
  autocomplete?: string;
  inputMode?: string;
  maxLength?: number;
  placeholder?: string;
  required?: boolean;
}): SecretInputRow {
  const row = document.createElement("div");
  row.className = "settings-dialog-token-row";
  const input = document.createElement("input");
  input.type = "password";
  if (options.autocomplete !== undefined) {
    input.setAttribute("autocomplete", options.autocomplete);
  }
  if (options.inputMode !== undefined) {
    input.setAttribute("inputmode", options.inputMode);
  }
  if (options.maxLength !== undefined) {
    input.maxLength = options.maxLength;
  }
  if (options.placeholder !== undefined) {
    input.placeholder = options.placeholder;
  }
  if (options.required === true) {
    input.required = true;
  }
  const revealBtn = document.createElement("button");
  revealBtn.type = "button";
  revealBtn.className = "btn settings-dialog-reveal";
  revealBtn.setAttribute("aria-label", "Show value");
  revealBtn.setAttribute("aria-pressed", "false");
  let revealed = false;
  const setRevealed = (next: boolean): void => {
    revealed = next;
    input.type = revealed ? "text" : "password";
    revealBtn.innerHTML = revealed ? SECRET_REVEAL_EYE_OFF_SVG : SECRET_REVEAL_EYE_SVG;
    revealBtn.setAttribute("aria-label", revealed ? "Hide value" : "Show value");
    revealBtn.setAttribute("aria-pressed", revealed ? "true" : "false");
  };
  setRevealed(false);
  revealBtn.addEventListener("click", () => {
    setRevealed(!revealed);
  });
  row.append(input, revealBtn);
  return { input, row, setRevealed };
}
