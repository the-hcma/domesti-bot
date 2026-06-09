// Small Leaflet map for participant location + geofence overlay (Status / Participants tabs).

import L from "leaflet";
import type { GeofenceOut } from "./types.js";

export interface PresenceMiniMapOptions {
  accuracy_m: number | null;
  geofences: GeofenceOut[];
  label: string;
  lat: number;
  lon: number;
}

export function mountPresenceMiniMap(
  mapEl: HTMLElement,
  options: PresenceMiniMapOptions,
): void {
  if (mapEl.dataset.leafletInit === "1") {
    return;
  }
  mapEl.dataset.leafletInit = "1";

  const map = L.map(mapEl, {
    attributionControl: false,
    dragging: false,
    scrollWheelZoom: false,
    zoomControl: false,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(map);

  const enabledGeofences = options.geofences.filter((g) => g.enabled);
  const boundsLayers: L.Layer[] = [];

  for (const geofence of enabledGeofences) {
    const circle = L.circle([geofence.center_lat, geofence.center_lon], {
      color: "var(--accent)",
      fillColor: "var(--accent)",
      fillOpacity: 0.1,
      radius: geofence.radius_m,
      weight: 2,
    }).addTo(map);
    boundsLayers.push(circle);
  }

  const marker = L.circleMarker([options.lat, options.lon], {
    color: "var(--fg)",
    fillColor: "var(--accent)",
    fillOpacity: 0.9,
    radius: 7,
    weight: 2,
  })
    .bindTooltip(options.label, { permanent: false })
    .addTo(map);
  boundsLayers.push(marker);

  if (options.accuracy_m !== null && options.accuracy_m > 0) {
    const accuracy = L.circle([options.lat, options.lon], {
      color: "var(--pending)",
      dashArray: "4 4",
      fillColor: "var(--pending)",
      fillOpacity: 0.08,
      radius: options.accuracy_m,
      weight: 1,
    }).addTo(map);
    boundsLayers.push(accuracy);
  }

  const boundsForLayer = (layer: L.Layer): L.LatLngBounds => {
    if (layer instanceof L.Circle) {
      return layer.getBounds();
    }
    if (layer instanceof L.CircleMarker) {
      const latlng = layer.getLatLng();
      return L.latLngBounds(latlng, latlng);
    }
    return L.latLngBounds([options.lat, options.lon], [options.lat, options.lon]);
  };

  if (boundsLayers.length > 0) {
    const first = boundsLayers[0];
    if (first !== undefined) {
      let bounds = boundsForLayer(first);
      for (const layer of boundsLayers.slice(1)) {
        bounds = bounds.extend(boundsForLayer(layer));
      }
      map.fitBounds(bounds, { padding: [12, 12] });
    }
  } else {
    map.setView([options.lat, options.lon], 14);
  }
}
