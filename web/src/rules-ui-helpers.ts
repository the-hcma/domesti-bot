// Shared Automations hub UI helpers (info badges, family labels, toggles).

export const FAMILY_ACTION_GROUP_LABELS: Record<string, string> = {
  kasa: "Lights & plugs",
  sonos: "Sonos zones",
  tailwind: "Garage doors",
};

let infoPopoverDocumentListenerInstalled = false;
let closeOpenInfoPopover: (() => void) | null = null;

function ensureInfoPopoverDocumentListener(): void {
  if (infoPopoverDocumentListenerInstalled) {
    return;
  }
  infoPopoverDocumentListenerInstalled = true;
  document.addEventListener("click", () => {
    if (closeOpenInfoPopover !== null) {
      closeOpenInfoPopover();
      closeOpenInfoPopover = null;
    }
  });
}

export function createInfoBadge(
  label: string,
  detail: string,
  example: string,
): HTMLSpanElement {
  ensureInfoPopoverDocumentListener();
  const wrap = document.createElement("span");
  wrap.className = "rules-info-badge-wrap";
  const badge = document.createElement("button");
  badge.type = "button";
  badge.className = "rules-info-badge";
  badge.setAttribute("aria-label", `About ${label}`);
  badge.setAttribute("aria-expanded", "false");
  badge.textContent = "i";
  const popover = document.createElement("span");
  popover.className = "rules-info-popover";
  popover.hidden = true;
  popover.setAttribute("role", "tooltip");
  const title = document.createElement("strong");
  title.textContent = label;
  const body = document.createElement("span");
  body.className = "rules-info-popover-body";
  body.textContent = detail;
  const ex = document.createElement("span");
  ex.className = "rules-info-popover-example";
  ex.textContent = `Example: ${example}`;
  popover.append(title, body, ex);
  badge.addEventListener("click", (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    const willOpen = popover.hidden;
    if (closeOpenInfoPopover !== null) {
      closeOpenInfoPopover();
      closeOpenInfoPopover = null;
    }
    if (willOpen) {
      popover.hidden = false;
      badge.setAttribute("aria-expanded", "true");
      closeOpenInfoPopover = () => {
        popover.hidden = true;
        badge.setAttribute("aria-expanded", "false");
      };
    } else {
      badge.setAttribute("aria-expanded", "false");
    }
  });
  wrap.append(badge, popover);
  return wrap;
}

export function createFieldLabel(
  text: string,
  info?: { detail: string; example: string },
): HTMLSpanElement {
  const row = document.createElement("span");
  row.className = "rules-field-label-row";
  const label = document.createElement("span");
  label.textContent = text;
  row.append(label);
  if (info !== undefined) {
    row.append(createInfoBadge(text, info.detail, info.example));
  }
  return row;
}

export function createEnableToggle(
  initial: boolean,
  onChange: (next: boolean) => void,
): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "rules-enable-toggle";
  const sync = (on: boolean): void => {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    btn.textContent = on ? "Enabled" : "Disabled";
    btn.classList.toggle("rules-enable-toggle-on", on);
  };
  sync(initial);
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();
    const next = btn.getAttribute("aria-pressed") !== "true";
    sync(next);
    onChange(next);
  });
  return btn;
}
