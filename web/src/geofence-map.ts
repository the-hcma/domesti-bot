// Geofence list + map editor (Leaflet/OSM). Map draw lands in PR3; form list in PR2.

import type { RulesDataSource } from "./rules-data-source.js";
import type { GeofenceOut, ParticipantStatusOut } from "./types.js";
import { runMyTracksSyncAction } from "./mytracks-sync-dialog.js";

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

  const syncRow = document.createElement("div");
  syncRow.className = "rules-geofences-sync";
  const syncMeta = document.createElement("p");
  syncMeta.className = "rules-card-meta";
  const syncedAt =
    sync.last_synced_at === null
      ? "never"
      : new Date(sync.last_synced_at).toLocaleString();
  syncMeta.textContent = `${sync.geofence_count} geofences · last synced ${syncedAt}`;
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
  syncRow.append(syncMeta, syncBtn);
  container.append(syncRow);

  const mapSlot = document.createElement("div");
  mapSlot.id = "rules-geofence-map";
  mapSlot.className = "rules-geofence-map";
  mapSlot.dataset.testid = "rules-geofence-map";
  container.append(mapSlot);
  await attachGeofenceLeafletMap(
    mapSlot,
    dataSource,
    onChanged,
    geofences,
    status.participants,
  );

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
      if (!window.confirm(`Delete geofence "${g.label}"?`)) {
        return;
      }
      void dataSource
        .deleteGeofence(g.geofence_id)
        .then(() => mountGeofenceMapPanel(container, dataSource, onChanged))
        .catch((err: unknown) => {
          window.alert(err instanceof Error ? err.message : String(err));
        });
    });
    actions.append(editBtn, delBtn);
    tbody.append(tr);
  }
  table.append(thead, tbody);

  const addBtn = document.createElement("button");
  addBtn.type = "button";
  addBtn.className = "btn";
  addBtn.textContent = "Add geofence";
  addBtn.addEventListener("click", () => {
    void showGeofenceForm(container, dataSource, onChanged, null, mapSlot);
  });

  container.append(table, addBtn);
}

async function attachGeofenceLeafletMap(
  mapSlot: HTMLElement,
  dataSource: RulesDataSource,
  onChanged: () => void | Promise<void>,
  geofences: GeofenceOut[],
  participants: ParticipantStatusOut[],
): Promise<void> {
  const { initGeofenceLeafletMap } = await import("./geofence-map-leaflet.js");
  await initGeofenceLeafletMap(
    mapSlot,
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
  const labelField = document.createElement("label");
  labelField.className = "settings-dialog-field";
  labelField.innerHTML = "<span>Label</span>";
  const labelInput = document.createElement("input");
  labelInput.value = existing?.label ?? "";
  labelInput.required = true;
  labelField.append(labelInput);
  const idField = document.createElement("label");
  idField.className = "settings-dialog-field";
  idField.innerHTML = "<span>Geofence id</span>";
  const idInput = document.createElement("input");
  idInput.value = existing?.geofence_id ?? "";
  idInput.required = true;
  idInput.readOnly = existing !== null;
  labelInput.addEventListener("input", () => {
    if (existing === null && !idInput.dataset.touched) {
      idInput.value = slugifyGeofenceId(labelInput.value);
    }
  });
  idInput.addEventListener("input", () => {
    idInput.dataset.touched = "1";
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
