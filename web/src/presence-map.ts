// Unified Leaflet map for participant locations + geofence overlays.

import L from "leaflet";
import { haversineM } from "./rules-mock-fixtures.js";
import { DEFAULT_MIN_FIX_ACCURACY_M } from "./rules-evaluate.js";
import type { GeofenceOut, ParticipantFixOut, ParticipantStatusOut } from "./types.js";

/** Extra meters beyond a geofence radius to still show a participant on the geofence map. */
export const NEARBY_GEOFENCE_BUFFER_M = 500;

/** ~Street-block scale when a single participant is selected. */
const SINGLE_PARTICIPANT_BOUNDS_RADIUS_M = 220;

const SINGLE_PARTICIPANT_MAX_ZOOM = 17;
const MULTI_PARTICIPANT_MAX_ZOOM = 15;

const PARTICIPANT_MARKER_COLORS = [
  "#2e7d32",
  "#1565c0",
  "#6a1b9a",
  "#c62828",
  "#ef6c00",
  "#00838f",
] as const;

export interface PresenceMapParticipant {
  age_seconds: number | null;
  display_name: string;
  inside_geofence_ids: string[];
  last_fix: ParticipantFixOut | null;
  participant_id: string;
  tracking_device_label: string;
}

export interface PresenceMapMountOptions {
  geofences: GeofenceOut[];
  participants: PresenceMapParticipant[];
  showParticipantFilters: boolean;
}

export interface PresenceMapController {
  destroy(): void;
}

interface ParticipantLayers {
  accuracy: L.Circle | null;
  marker: L.CircleMarker;
}

export function formatAge(seconds: number | null): string {
  if (seconds === null) {
    return "never";
  }
  if (seconds < 60) {
    return `${seconds}s ago`;
  }
  if (seconds < 3600) {
    return `${Math.floor(seconds / 60)} min ago`;
  }
  return `${Math.floor(seconds / 3600)} h ago`;
}

export function formatParticipantTooltipHtml(participant: PresenceMapParticipant): string {
  const lines: string[] = [`<strong>${escapeHtml(participant.display_name)}</strong>`];
  lines.push(`Tracking device: ${escapeHtml(participant.tracking_device_label)}`);
  const inside =
    participant.inside_geofence_ids.length > 0
      ? `Inside ${participant.inside_geofence_ids.join(", ")}`
      : "Outside all geofences";
  lines.push(`${formatAge(participant.age_seconds)} · ${escapeHtml(inside)}`);
  const fix = participant.last_fix;
  if (fix !== null) {
    const accuracy = fix.accuracy_m === null ? "unknown" : `±${fix.accuracy_m} m`;
    lines.push(
      `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)} · ${escapeHtml(accuracy)}`,
    );
    if (
      fix.accuracy_m !== null
      && fix.accuracy_m > DEFAULT_MIN_FIX_ACCURACY_M
    ) {
      lines.push(
        `Low accuracy — ignored by rules (&gt;${DEFAULT_MIN_FIX_ACCURACY_M} m)`,
      );
    }
  } else {
    lines.push("No location fix yet");
  }
  return lines.join("<br>");
}

export function participantNearEnabledGeofence(
  fix: ParticipantFixOut,
  geofences: GeofenceOut[],
): boolean {
  for (const geofence of geofences) {
    if (!geofence.enabled) {
      continue;
    }
    const dist = haversineM(
      fix.lat,
      fix.lon,
      geofence.center_lat,
      geofence.center_lon,
    );
    if (dist <= geofence.radius_m + NEARBY_GEOFENCE_BUFFER_M) {
      return true;
    }
  }
  return false;
}

export function mountPresenceMap(
  rootEl: HTMLElement,
  options: PresenceMapMountOptions,
): PresenceMapController {
  rootEl.replaceChildren();

  const visibleIds = new Set(
    options.participants.map((p) => p.participant_id),
  );

  const filtersEl = document.createElement("div");
  filtersEl.className = "rules-presence-map-filters";
  if (options.showParticipantFilters) {
    for (const participant of options.participants) {
      const label = document.createElement("label");
      label.className = "rules-presence-map-filter";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.dataset.participantId = participant.participant_id;
      const name = document.createElement("span");
      name.textContent = participant.display_name;
      label.append(checkbox, name);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          visibleIds.add(participant.participant_id);
        } else {
          visibleIds.delete(participant.participant_id);
        }
        syncParticipantVisibility();
        fitVisibleBounds();
      });
      filtersEl.append(label);
    }
  } else {
    filtersEl.hidden = true;
  }

  const mapEl = document.createElement("div");
  mapEl.className = "rules-presence-map";
  rootEl.append(filtersEl, mapEl);

  const map = L.map(mapEl, {
    attributionControl: false,
    scrollWheelZoom: true,
    zoomControl: true,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(map);

  const geofenceLayers: L.Circle[] = [];
  for (const geofence of options.geofences) {
    if (!geofence.enabled) {
      continue;
    }
    const circle = L.circle([geofence.center_lat, geofence.center_lon], {
      color: "var(--accent)",
      fillColor: "var(--accent)",
      fillOpacity: 0.1,
      radius: geofence.radius_m,
      weight: 2,
    })
      .bindTooltip(geofence.label, { permanent: false })
      .addTo(map);
    geofenceLayers.push(circle);
  }

  const participantLayerById = new Map<string, ParticipantLayers>();
  options.participants.forEach((participant, index) => {
    const fix = participant.last_fix;
    if (fix === null) {
      return;
    }
    const color =
      PARTICIPANT_MARKER_COLORS[index % PARTICIPANT_MARKER_COLORS.length]
      ?? PARTICIPANT_MARKER_COLORS[0];
    const marker = L.circleMarker([fix.lat, fix.lon], {
      color: "var(--fg)",
      fillColor: color,
      fillOpacity: 0.9,
      radius: 8,
      weight: 2,
    })
      .bindTooltip(formatParticipantTooltipHtml(participant), {
        className: "rules-presence-map-tooltip",
        direction: "top",
        sticky: true,
      })
      .addTo(map);
    let accuracy: L.Circle | null = null;
    if (fix.accuracy_m !== null && fix.accuracy_m > 0) {
      accuracy = L.circle([fix.lat, fix.lon], {
        color: "var(--pending)",
        dashArray: "4 4",
        fillColor: "var(--pending)",
        fillOpacity: 0.08,
        radius: fix.accuracy_m,
        weight: 1,
      }).addTo(map);
    }
    participantLayerById.set(participant.participant_id, { accuracy, marker });
  });

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

  const syncParticipantVisibility = (): void => {
    for (const [participantId, layers] of participantLayerById) {
      const show = visibleIds.has(participantId);
      const opacity = show ? 1 : 0;
      layers.marker.setStyle({ fillOpacity: show ? 0.9 : 0, opacity });
      if (layers.accuracy !== null) {
        layers.accuracy.setStyle({ fillOpacity: show ? 0.08 : 0, opacity });
      }
    }
  };

  const fitVisibleBounds = (): void => {
    const boundsLayers: L.Layer[] = [...geofenceLayers];
    for (const [participantId, layers] of participantLayerById) {
      if (!visibleIds.has(participantId)) {
        continue;
      }
      boundsLayers.push(layers.marker);
      if (layers.accuracy !== null) {
        boundsLayers.push(layers.accuracy);
      }
    }

    const visibleWithFix = options.participants.filter(
      (p) => visibleIds.has(p.participant_id) && p.last_fix !== null,
    );

    if (visibleWithFix.length === 1) {
      const fix = visibleWithFix[0]?.last_fix;
      if (fix !== undefined && fix !== null) {
        const center = L.latLng(fix.lat, fix.lon);
        map.fitBounds(center.toBounds(SINGLE_PARTICIPANT_BOUNDS_RADIUS_M), {
          maxZoom: SINGLE_PARTICIPANT_MAX_ZOOM,
          padding: [24, 24],
        });
        return;
      }
    }

    if (boundsLayers.length > 0) {
      const first = boundsLayers[0];
      if (first !== undefined) {
        let bounds = boundsForLayer(first);
        for (const layer of boundsLayers.slice(1)) {
          bounds = bounds.extend(boundsForLayer(layer));
        }
        map.fitBounds(bounds, {
          maxZoom: MULTI_PARTICIPANT_MAX_ZOOM,
          padding: [32, 32],
        });
        return;
      }
    }

    const fallback = options.geofences.find((g) => g.enabled);
    if (fallback !== undefined) {
      map.setView([fallback.center_lat, fallback.center_lon], 14);
    }
  };

  syncParticipantVisibility();
  fitVisibleBounds();
  window.requestAnimationFrame(() => {
    map.invalidateSize();
    fitVisibleBounds();
  });

  return {
    destroy(): void {
      map.remove();
      rootEl.replaceChildren();
    },
  };
}

export function participantStatusToMapParticipant(
  participant: ParticipantStatusOut,
): PresenceMapParticipant {
  return {
    age_seconds: participant.age_seconds,
    display_name: participant.display_name,
    inside_geofence_ids: participant.inside_geofence_ids,
    last_fix: participant.last_fix,
    participant_id: participant.participant_id,
    tracking_device_label: participant.tracking_device_label,
  };
}

export function renderParticipantDetailText(
  participant: PresenceMapParticipant,
  includeParticipantId: boolean,
): HTMLElement {
  const card = document.createElement("article");
  card.className = "rules-card rules-participant-detail-card";
  const name = document.createElement("strong");
  name.textContent = includeParticipantId
    ? `${participant.display_name} (${participant.participant_id})`
    : participant.display_name;
  const deviceMeta = document.createElement("p");
  deviceMeta.className = "rules-card-meta";
  deviceMeta.textContent = `Tracking device: ${participant.tracking_device_label}`;
  const meta = document.createElement("p");
  meta.className = "rules-card-meta";
  const inside =
    participant.inside_geofence_ids.length > 0
      ? `Inside ${participant.inside_geofence_ids.join(", ")}`
      : "Outside all geofences";
  meta.textContent = `${formatAge(participant.age_seconds)} · ${inside}`;
  card.append(name, deviceMeta, meta);
  const fix = participant.last_fix;
  if (fix !== null) {
    const coords = document.createElement("p");
    coords.className = "rules-card-meta";
    const accuracy = fix.accuracy_m === null ? "unknown" : `±${fix.accuracy_m} m`;
    coords.textContent = `${fix.lat.toFixed(5)}, ${fix.lon.toFixed(5)} · ${accuracy}`;
    card.append(coords);
    if (
      fix.accuracy_m !== null
      && fix.accuracy_m > DEFAULT_MIN_FIX_ACCURACY_M
    ) {
      const warn = document.createElement("p");
      warn.className = "rules-card-warn";
      warn.textContent = `Low accuracy — ignored by rules (>${DEFAULT_MIN_FIX_ACCURACY_M} m)`;
      card.append(warn);
    }
  }
  return card;
}

function escapeHtml(raw: string): string {
  return raw
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
