// Tabbed Settings hub (GoTailwind token + My Tracks connection).

import { createRulesDataSource } from "./rules-data-source.js";
import { mountMyTracksSettingsPanel } from "./my-tracks-settings-panel.js";
import { mountTailwindSettingsPanel } from "./tailwind-settings-panel.js";

type SettingsTabId = "my-tracks" | "tailwind";

const SETTINGS_TABS: readonly [SettingsTabId, string][] = [
  ["tailwind", "GoTailwind"],
  ["my-tracks", "My Tracks"],
];

export async function openSettingsHubDialog(options: {
  onReloadDevices?: () => void | Promise<void>;
}): Promise<void> {
  const dialog = document.createElement("dialog");
  dialog.className = "settings-dialog settings-hub-dialog";

  const panel = document.createElement("div");
  panel.className = "settings-dialog-panel";

  const header = document.createElement("header");
  header.className = "settings-dialog-header";
  const title = document.createElement("h2");
  title.textContent = "Settings";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "settings-dialog-close";
  closeBtn.setAttribute("aria-label", "Close");
  closeBtn.textContent = "\u00d7";
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  header.append(title, closeBtn);

  const tabBar = document.createElement("div");
  tabBar.className = "rules-tab-bar settings-tab-bar";
  tabBar.setAttribute("role", "tablist");

  const body = document.createElement("div");
  body.className = "settings-dialog-body settings-hub-body";

  let activeTab: SettingsTabId = "tailwind";
  const tabButtons = new Map<SettingsTabId, HTMLButtonElement>();

  for (const [tabId, label] of SETTINGS_TABS) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "rules-tab";
    btn.dataset.tab = tabId;
    btn.setAttribute("role", "tab");
    btn.textContent = label;
    btn.addEventListener("click", () => {
      void setTab(tabId);
    });
    tabBar.append(btn);
    tabButtons.set(tabId, btn);
  }

  panel.append(header, tabBar, body);
  dialog.append(panel);
  document.body.append(dialog);

  dialog.addEventListener("close", () => {
    dialog.remove();
  });
  dialog.addEventListener("click", (ev) => {
    if (ev.target === dialog) {
      dialog.close();
    }
  });
  dialog.addEventListener("cancel", (ev) => {
    ev.preventDefault();
    dialog.close();
  });

  async function setTab(tabId: SettingsTabId): Promise<void> {
    activeTab = tabId;
    for (const [id, btn] of tabButtons) {
      const selected = id === tabId;
      btn.classList.toggle("rules-tab-active", selected);
      btn.setAttribute("aria-selected", selected ? "true" : "false");
    }
    body.replaceChildren();
    const mount = document.createElement("div");
    mount.className = "settings-hub-tab-mount";
    body.append(mount);
    if (tabId === "my-tracks") {
      const dataSource = await createRulesDataSource();
      await mountMyTracksSettingsPanel(mount, dataSource);
      return;
    }
    await mountTailwindSettingsPanel(mount, {
      onDevicesChanged: options.onReloadDevices,
    });
  }

  dialog.showModal();
  await setTab(activeTab);
}
