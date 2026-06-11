// Leaflet + OpenStreetMap geofence editor (ported from my-tracks ``geofences.html``).

import L from "leaflet";
import { userMarkerColor } from "./map-device-colors.js";
import {
  createShellTooltipController,
  formatUserTooltipHtml,
  userNearEnabledGeofence,
  userStatusToMapUser,
  type PresenceMapUser,
} from "./presence-map.js";
import type { RulesDataSource } from "./rules-data-source.js";
import type { GeofenceDrawToolbar } from "./geofence-map.js";
import type { GeofenceOut, UserStatusOut, SettingsLocationOut } from "./types.js";

type DrawState = "idle" | "placing-center" | "placing-radius";

function slugifyGeofenceId(label: string): string {
  return label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

export async function initGeofenceLeafletMap(
  mapEl: HTMLElement,
  panel: HTMLElement,
  drawGroup: HTMLElement,
  toolbar: GeofenceDrawToolbar,
  dataSource: RulesDataSource,
  onChanged: () => void | Promise<void>,
  geofences: GeofenceOut[],
  users: UserStatusOut[] = [],
): Promise<void> {
  if (mapEl.dataset.leafletInit === "1") {
    return;
  }
  mapEl.dataset.leafletInit = "1";

  const settings: SettingsLocationOut = await dataSource.getSettingsLocation();
  const defaultCenter: L.LatLngExpression =
    settings.lat !== 0 || settings.lon !== 0
      ? [settings.lat, settings.lon]
      : geofences[0]
        ? [geofences[0].center_lat, geofences[0].center_lon]
        : [20, 0];
  const defaultZoom = geofences.length > 0 || (settings.lat !== 0 && settings.lon !== 0) ? 14 : 2;

  const map = L.map(mapEl, { zoomControl: true, attributionControl: false });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(map);
  map.setView(defaultCenter, defaultZoom);

  const tooltipShell = mapEl.parentElement ?? mapEl;
  const shellTooltip = createShellTooltipController(tooltipShell, map);

  if (settings.home_label !== null && settings.lat !== 0) {
    const homeIcon = L.divIcon({
      html: "\u{1f3e0}",
      className: "",
      iconSize: [20, 20],
      iconAnchor: [10, 10],
    });
    L.marker([settings.lat, settings.lon], { icon: homeIcon })
      .bindTooltip(settings.home_label, { permanent: false })
      .addTo(map);
  }

  const circleLayer: Record<string, L.Circle> = {};
  for (const g of geofences) {
    if (!g.enabled) {
      continue;
    }
    const circle = L.circle([g.center_lat, g.center_lon], {
      radius: g.radius_m,
      color: "var(--accent)",
      fillColor: "var(--accent)",
      fillOpacity: 0.12,
      weight: 2,
    })
      .bindTooltip(g.label, { permanent: false })
      .addTo(map);
    circleLayer[g.geofence_id] = circle;
  }

  const nearbyUsers = users
    .map(userStatusToMapUser)
    .filter(
      (user): user is PresenceMapUser & {
        last_location: NonNullable<PresenceMapUser["last_location"]>;
      } =>
        user.last_location !== null
        && userNearEnabledGeofence(user.last_location, geofences),
    );
  const userMarkers: L.CircleMarker[] = [];
  for (const user of nearbyUsers) {
    const location = user.last_location;
    const color = userMarkerColor(
      user.tracking_device_label,
      user.user_id,
    );
    const tooltipHtml = formatUserTooltipHtml(user);
    const marker = L.circleMarker([location.lat, location.lon], {
      className: "rules-presence-user-marker",
      color: "#fff",
      fillColor: color,
      fillOpacity: 0.9,
      opacity: 1,
      radius: 10,
      weight: 2,
    }).addTo(map);
    shellTooltip.attach(marker, () => tooltipHtml);
    userMarkers.push(marker);
  }

  const boundsLayers: L.Layer[] = [...Object.values(circleLayer), ...userMarkers];
  const firstBoundsLayer = boundsLayers[0];
  if (firstBoundsLayer !== undefined) {
    const boundsForLayer = (layer: L.Layer): L.LatLngBounds => {
      if (layer instanceof L.CircleMarker) {
        const latlng = layer.getLatLng();
        return L.latLngBounds(latlng, latlng);
      }
      if (layer instanceof L.Circle) {
        return layer.getBounds();
      }
      return map.getBounds();
    };
    let bounds = boundsForLayer(firstBoundsLayer);
    for (const layer of boundsLayers.slice(1)) {
      bounds = bounds.extend(boundsForLayer(layer));
    }
    map.fitBounds(bounds, { padding: [40, 40], maxZoom: 15 });
  }

  const drawBtn = document.createElement("button");
  drawBtn.type = "button";
  drawBtn.className = "btn";
  drawBtn.textContent = "Draw geofence";
  const cancelDrawBtn = document.createElement("button");
  cancelDrawBtn.type = "button";
  cancelDrawBtn.className = "btn btn-secondary";
  cancelDrawBtn.textContent = "Cancel draw";
  cancelDrawBtn.hidden = true;
  toolbar.drawActions.replaceChildren(drawBtn, cancelDrawBtn);

  let drawState: DrawState = "idle";
  let drawCenter: L.LatLng | null = null;
  let previewCircle: L.Circle | null = null;
  let previewMarker: L.CircleMarker | null = null;
  let formPanel: HTMLDivElement | null = null;

  const clearPreview = (): void => {
    if (previewCircle !== null) {
      map.removeLayer(previewCircle);
      previewCircle = null;
    }
    if (previewMarker !== null) {
      map.removeLayer(previewMarker);
      previewMarker = null;
    }
    drawCenter = null;
  };

  const exitDraw = (): void => {
    drawState = "idle";
    mapEl.classList.remove("rules-geofence-draw-mode");
    panel.classList.remove("rules-geofence-draw-active");
    drawGroup.classList.remove("rules-geofence-toolbar-draw-active");
    drawBtn.hidden = false;
    cancelDrawBtn.hidden = true;
    toolbar.drawHint.textContent = "";
    clearPreview();
  };

  const showDrawForm = (lat: number, lon: number, radius: number): void => {
    formPanel?.remove();
    formPanel = document.createElement("div");
    formPanel.className = "rules-geofence-form-panel";
    const form = document.createElement("form");
    const labelField = document.createElement("label");
    labelField.className = "settings-dialog-field";
    labelField.innerHTML = "<span>Label</span>";
    const labelInput = document.createElement("input");
    labelInput.required = true;
    labelField.append(labelInput);
    const idField = document.createElement("label");
    idField.className = "settings-dialog-field";
    idField.innerHTML = "<span>Geofence id</span>";
    const idInput = document.createElement("input");
    idInput.required = true;
    labelInput.addEventListener("input", () => {
      idInput.value = slugifyGeofenceId(labelInput.value);
    });
    idField.append(idInput);
    const latInput = document.createElement("input");
    latInput.type = "hidden";
    latInput.value = String(lat);
    const lonInput = document.createElement("input");
    lonInput.type = "hidden";
    lonInput.value = String(lon);
    const radiusInput = document.createElement("input");
    radiusInput.type = "hidden";
    radiusInput.value = String(radius);
    const actions = document.createElement("div");
    actions.className = "settings-dialog-actions";
    const cancelBtn = document.createElement("button");
    cancelBtn.type = "button";
    cancelBtn.className = "btn";
    cancelBtn.textContent = "Cancel";
    cancelBtn.addEventListener("click", () => {
      formPanel?.remove();
      formPanel = null;
      exitDraw();
    });
    const saveBtn = document.createElement("button");
    saveBtn.type = "submit";
    saveBtn.className = "btn";
    saveBtn.textContent = "Save";
    actions.append(cancelBtn, saveBtn);
    form.append(labelField, idField, latInput, lonInput, radiusInput, actions);
    form.addEventListener("submit", (ev) => {
      ev.preventDefault();
      const geofence: GeofenceOut = {
        geofence_id: idInput.value.trim(),
        label: labelInput.value.trim(),
        center_lat: lat,
        center_lon: lon,
        radius_m: radius,
        enabled: true,
        owntracks_rid: null,
      };
      void dataSource.saveGeofence(geofence).then(async () => {
        formPanel?.remove();
        formPanel = null;
        exitDraw();
        await onChanged();
      });
    });
    formPanel.append(form);
    mapEl.insertAdjacentElement("afterend", formPanel);
  };

  drawBtn.addEventListener("click", () => {
    drawState = "placing-center";
    mapEl.classList.add("rules-geofence-draw-mode");
    panel.classList.add("rules-geofence-draw-active");
    drawGroup.classList.add("rules-geofence-toolbar-draw-active");
    drawBtn.hidden = true;
    cancelDrawBtn.hidden = false;
    toolbar.drawHint.textContent = "Click the map to place the geofence center.";
  });

  cancelDrawBtn.addEventListener("click", () => {
    exitDraw();
  });

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && drawState !== "idle") {
      exitDraw();
    }
  });

  map.on("click", (e: L.LeafletMouseEvent) => {
    if (drawState === "placing-center") {
      drawCenter = e.latlng;
      drawState = "placing-radius";
      toolbar.drawHint.textContent = "Click again to set the radius.";
      previewMarker = L.circleMarker(drawCenter, {
        radius: 5,
        color: "var(--accent)",
        fillColor: "var(--accent)",
        fillOpacity: 1,
        weight: 2,
      }).addTo(map);
      previewCircle = L.circle(drawCenter, {
        radius: 50,
        color: "var(--accent)",
        fillColor: "var(--accent)",
        fillOpacity: 0.15,
        weight: 2,
        dashArray: "6 4",
      }).addTo(map);
    } else if (drawState === "placing-radius" && drawCenter !== null) {
      const radius = Math.max(10, Math.round(drawCenter.distanceTo(e.latlng)));
      if (previewCircle !== null) {
        map.removeLayer(previewCircle);
      }
      previewCircle = L.circle(drawCenter, {
        radius,
        color: "var(--accent)",
        fillColor: "var(--accent)",
        fillOpacity: 0.15,
        weight: 2,
      }).addTo(map);
      showDrawForm(drawCenter.lat, drawCenter.lng, radius);
      drawState = "idle";
      mapEl.classList.remove("rules-geofence-draw-mode");
      panel.classList.remove("rules-geofence-draw-active");
      drawGroup.classList.remove("rules-geofence-toolbar-draw-active");
      drawBtn.hidden = false;
      cancelDrawBtn.hidden = true;
      toolbar.drawHint.textContent = "";
    }
  });

  map.on("mousemove", (e: L.LeafletMouseEvent) => {
    if (drawState === "placing-radius" && drawCenter !== null && previewCircle !== null) {
      const radius = Math.max(10, Math.round(drawCenter.distanceTo(e.latlng)));
      previewCircle.setRadius(radius);
    }
  });
}
