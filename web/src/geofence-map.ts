// Geofence list + map editor (Leaflet/OSM). Map draw lands in PR3; form list in PR2.

import type { RulesDataSource } from "./rules-data-source.js";
import type { GeofenceOut, ParticipantStatusOut } from "./types.js";
import { runMyTracksSyncAction } from "./mytracks-sync-dialog.js";
import { confirmAction, showErrorToast } from "./ui-toast.js";

function slugifyGeofenceId(label: string): string {
  return label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export async function mountGeofenceMapPanel(
  container: HTMLElement,
  dataSource: RulesDataSource,
  onChanged: () => void | Promise<void>,
): Promise<void> {
  container.replaceChildren();
  const geofences = await dataSource.listGeofences();
  const status = await dataSource.getStatus();
  const sync = await dataSource.getMyTracksGeofencesSync();

  const panel = document.createElement("div");
  panel.className = "rules-geofence-panel";
  panel.setAttribute("autocomplete", "off");

  const toolbar = document.createElement("div");
  toolbar.className = "rules-geofence-toolbar";

  const importGroup = document.createElement("div");
  importGroup.className = "rules-geofence-toolbar-group rules-geofence-toolbar-import";
  const importLabel = document.createElement("span");
  importLabel.className = "rules-geofence-toolbar-group-label";
  importLabel.textContent = "Import";
  const syncBtn = document.createElement("button");
  syncBtn.type = "button";
  syncBtn.className = "btn btn-secondary";
  syncBtn.textContent = "Sync from My Tracks";
  syncBtn.dataset.testid = "rules-geofences-sync-btn";
  syncBtn.addEventListener("click", () => {
    void runMyTracksSyncAction(dataSource, "geofences", () =>
      mountGeofenceMapPanel(container, dataSource, onChanged),
    );
  });
  const syncMeta = document.createElement("span");
  syncMeta.className = "rules-geofence-sync-meta";
  const syncedAt =
    sync.last_synced_at === null
      ? "never"
      : new Date(sync.last_synced_at).toLocaleString();
  syncMeta.textContent = `${sync.geofence_count} geofences · last synced ${syncedAt}`;
  importGroup.append(importLabel, syncBtn, syncMeta);

  const drawGroup = document.createElement("div");
  drawGroup.className = "rules-geofence-toolbar-group rules-geofence-toolbar-draw";
  const drawLabel = document.createElement("span");
  drawLabel.className = "rules-geofence-toolbar-group-label";
  drawLabel.textContent = "Draw";
  const drawActions = document.createElement("div");
  drawActions.className = "rules-geofence-toolbar-draw-actions";
  const drawHint = document.createElement("span");
  drawHint.className = "rules-geofence-draw-hint";
  drawGroup.append(drawLabel, drawActions, drawHint);

  toolbar.append(importGroup, drawGroup);
  panel.append(toolbar);

  const mapWrap = document.createElement("div");
  mapWrap.className = "rules-geofence-map-wrap";
  const mapSlot = document.createElement("div");
  mapSlot.id = "rules-geofence-map";
  mapSlot.className = "rules-geofence-map";
  mapSlot.dataset.testid = "rules-geofence-map";
  mapWrap.append(mapSlot);
  panel.append(mapWrap);
  container.append(panel);

  await attachGeofenceLeafletMap(
    mapSlot,
    panel,
    drawGroup,
    {
      drawActions,
      drawHint,
    },
    dataSource,
    onChanged,
    geofences,
    status.participants,
  );

  const listSection = document.createElement("section");
  listSection.className = "rules-geofence-list-section";
  const listHeading = document.createElement("h3");
  listHeading.className = "rules-geofence-list-heading";
  listHeading.textContent = "Saved geofences";
  listSection.append(listHeading);

  const table = document.createElement("table");
  table.className = "rules-geofence-table";
  const thead = document.createElement("thead");
  thead.innerHTML = "<tr><th>Label</th><th>Center</th><th>Radius</th><th></th></tr>";
  const tbody = document.createElement("tbody");
  for (const g of geofences) {
    const tr = document.createElement("tr");
    tr.dataset.geofenceId = g.geofence_id;
    tr.innerHTML = `<td>${g.label}</td><td>${g.center_lat.toFixed(5)}, ${g.center_lon.toFixed(5)}</td><td>${g.radius_m} m</td><td></td>`;
    const actions = tr.lastElementChild as HTMLTableCellElement;
    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "btn btn-secondary";
    editBtn.textContent = "Edit";
    editBtn.addEventListener("click", () => {
      void showGeofenceForm(container, dataSource, onChanged, g, mapSlot);
    });
    const delBtn = document.createElement("button");
    delBtn.type = "button";
    delBtn.className = "btn btn-danger";
    delBtn.textContent = "Delete";
    delBtn.addEventListener("click", () => {
      void confirmAction({
        message: `Delete geofence "${g.label}"?`,
        confirmLabel: "Delete",
        variant: "danger",
      }).then((confirmed) => {
        if (!confirmed) {
          return;
        }
        void dataSource
          .deleteGeofence(g.geofence_id)
          .then(() => mountGeofenceMapPanel(container, dataSource, onChanged))
          .catch((err: unknown) => {
            showErrorToast(err instanceof Error ? err.message : String(err));
          });
      });
    });
    actions.append(editBtn, delBtn);
    tbody.append(tr);
  }
  table.append(thead, tbody);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn btn-secondary rules-geofence-add-btn";
  addBtn.textContent = "Add manually";
  addBtn.addEventListener("click", () => {
    void showGeofenceForm(container, dataSource, onChanged, null, mapSlot);
  });

  listSection.append(table, addBtn);
  container.append(listSection);
}

export interface GeofenceDrawToolbar {
  drawActions: HTMLElement;
  drawHint: HTMLElement;
}

async function attachGeofenceLeafletMap(
  mapSlot: HTMLElement,
  panel: HTMLElement,
  drawGroup: HTMLElement,
  toolbar: GeofenceDrawToolbar,
  dataSource: RulesDataSource,
  onChanged: () => void | Promise<void>,
  geofences: GeofenceOut[],
  participants: ParticipantStatusOut[],
): Promise<void> {
  const { initGeofenceLeafletMap } = await import("./geofence-map-leaflet.js");
  await initGeofenceLeafletMap(
    mapSlot,
    panel,
    drawGroup,
    toolbar,
    dataSource,
    onChanged,
    geofences,
    participants,
  );
}

async function showGeofenceForm(
  container: HTMLElement,
  dataSource: RulesDataSource,
  onChanged: () => void | Promise<void>,
  existing: GeofenceOut | null,
  mapSlot: HTMLElement,
): Promise<void> {
  const panel = document.createElement("div");
  panel.className = "rules-geofence-form-panel";
  const form = document.createElement("form");
  form.setAttribute("autocomplete", "off");
  const labelField = document.createElement("label");
  labelField.className = "settings-dialog-field";
  labelField.innerHTML = "<span>Label</span>";
  const labelInput = document.createElement("input");
  labelInput.value = existing?.label ?? "";
  labelInput.required = true;
  labelInput.setAttribute("autocomplete", "off");
  labelField.append(labelInput);
  const idField = document.createElement("label");
  idField.className = "settings-dialog-field";
  idField.innerHTML = "<span>Geofence id</span>";
  const idInput = document.createElement("input");
  idInput.value = existing?.geofence_id ?? "";
  idInput.required = true;
  idInput.readOnly = existing !== null;
  idInput.setAttribute("autocomplete", "off");
  labelInput.addEventListener("input", () => {
    if (existing === null && !idInput.dataset.touched) {
      idInput.value = slugifyGeofenceId(labelInput.value);
    }
  });
  idInput.addEventListener("input", (ev) => {
    if (ev.isTrusted) {
      idInput.dataset.touched = "1";
    }
  });
  idField.append(idInput);
  const latField = document.createElement("label");
  latField.className = "settings-dialog-field";
  latField.innerHTML = "<span>Latitude</span>";
  const latInput = document.createElement("input");
  latInput.type = "number";
  latInput.step = "any";
  latInput.value = String(existing?.center_lat ?? "");
  latInput.required = true;
  latField.append(latInput);
  const lonField = document.createElement("label");
  lonField.className = "settings-dialog-field";
  lonField.innerHTML = "<span>Longitude</span>";
  const lonInput = document.createElement("input");
  lonInput.type = "number";
  lonInput.step = "any";
  lonInput.value = String(existing?.center_lon ?? "");
  lonInput.required = true;
  lonField.append(lonInput);
  const radiusField = document.createElement("label");
  radiusField.className = "settings-dialog-field";
  radiusField.innerHTML = "<span>Radius (m)</span>";
  const radiusInput = document.createElement("input");
  radiusInput.type = "number";
  radiusInput.min = "10";
  radiusInput.max = "50000";
  radiusInput.value = String(existing?.radius_m ?? 250);
  radiusInput.required = true;
  radiusField.append(radiusInput);
  const actions = document.createElement("div");
  actions.className = "settings-dialog-actions";
  const cancelBtn = document.createElement("button");
  cancelBtn.type = "button";
  cancelBtn.className = "btn";
  cancelBtn.textContent = "Cancel";
  cancelBtn.addEventListener("click", () => {
    panel.remove();
  });
  const saveBtn = document.createElement("button");
  saveBtn.type = "submit";
  saveBtn.className = "btn";
  saveBtn.textContent = "Save";
  actions.append(cancelBtn, saveBtn);
  form.append(labelField, idField, latField, lonField, radiusField, actions);
  form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const geofence: GeofenceOut = {
      geofence_id: idInput.value.trim(),
      label: labelInput.value.trim(),
      center_lat: Number(latInput.value),
      center_lon: Number(lonInput.value),
      radius_m: Number(radiusInput.value),
      enabled: existing?.enabled ?? true,
      owntracks_rid: existing?.owntracks_rid ?? null,
    };
    void dataSource.saveGeofence(geofence).then(async () => {
      panel.remove();
      await mountGeofenceMapPanel(container, dataSource, onChanged);
      await onChanged();
    });
  });
  panel.append(form);
  mapSlot.insertAdjacentElement("afterend", panel);
}
