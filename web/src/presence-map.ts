// Unified Leaflet map for participant locations + geofence overlays.

import L from "leaflet";
import { formatLocalTimestamp, formatUtcTimestampTitle } from "./format-timestamp.js";
import { participantMarkerColor } from "./map-device-colors.js";
import { haversineM } from "./rules-mock-fixtures.js";
import { DEFAULT_MIN_FIX_ACCURACY_M } from "./rules-evaluate.js";
import type { GeofenceOut, ParticipantFixOut, ParticipantStatusOut } from "./types.js";

/** Extra meters beyond a geofence radius to still show a participant on the geofence map. */
export const NEARBY_GEOFENCE_BUFFER_M = 500;

/** ~Street-block scale when a single participant is selected. */
const SINGLE_PARTICIPANT_BOUNDS_RADIUS_M = 220;

const SINGLE_PARTICIPANT_MAX_ZOOM = 17;
const MULTI_PARTICIPANT_MAX_ZOOM = 15;

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
  /** When true, tooltip title includes ``participant_id`` (Participants tab). */
  includeParticipantIdInTooltip?: boolean;
  participants: PresenceMapParticipant[];
  showParticipantFilters: boolean;
}

export interface PresenceMapController {
  destroy(): void;
  updateParticipants(participants: PresenceMapParticipant[]): void;
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

export function formatParticipantTooltipHtml(
  participant: PresenceMapParticipant,
  options?: { includeParticipantId?: boolean },
): string {
  const title = options?.includeParticipantId === true
    ? `${participant.display_name} (${participant.participant_id})`
    : participant.display_name;
  const lines: string[] = [`<strong>${escapeHtml(title)}</strong>`];
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
    lines.push(
      `Fix at <span title="${escapeHtml(formatUtcTimestampTitle(fix.received_at))}">`
        + `${escapeHtml(formatLocalTimestamp(fix.received_at))}</span>`,
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
  return lines
    .map((line) => `<span class="rules-presence-map-tooltip-line">${line}</span>`)
    .join("");
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

function participantMarkerOptions(color: string): L.CircleMarkerOptions {
  return {
    className: "rules-presence-participant-marker",
    color: "#fff",
    fillColor: color,
    fillOpacity: 0.9,
    opacity: 1,
    radius: 10,
    weight: 2,
  };
}

function renderPresenceMapLegend(
  legendEl: HTMLElement,
  participants: PresenceMapParticipant[],
  visibleIds: ReadonlySet<string>,
): void {
  legendEl.replaceChildren();
  const withFix = participants.filter((participant) => participant.last_fix !== null);
  if (withFix.length < 2) {
    legendEl.hidden = true;
    return;
  }
  legendEl.hidden = false;

  const title = document.createElement("div");
  title.className = "rules-presence-map-legend-title";
  title.textContent = "Participants";
  legendEl.append(title);

  const sorted = [...withFix].sort((left, right) =>
    left.display_name.localeCompare(right.display_name, undefined, {
      sensitivity: "base",
    }),
  );
  for (const participant of sorted) {
    const color = participantMarkerColor(
      participant.tracking_device_label,
      participant.participant_id,
    );
    const item = document.createElement("div");
    item.className = "rules-presence-map-legend-item";
    if (!visibleIds.has(participant.participant_id)) {
      item.classList.add("rules-presence-map-legend-item-hidden");
    }

    const swatch = document.createElement("div");
    swatch.className = "rules-presence-map-legend-color";
    swatch.style.backgroundColor = color;
    item.append(swatch);

    const labels = document.createElement("div");
    labels.className = "rules-presence-map-legend-labels";
    const name = document.createElement("span");
    name.className = "rules-presence-map-legend-name";
    name.textContent = participant.display_name;
    const device = document.createElement("span");
    device.className = "rules-presence-map-legend-device";
    device.textContent = participant.tracking_device_label;
    labels.append(name, device);
    item.append(labels);
    legendEl.append(item);
  }
}

export function mountPresenceMap(
  rootEl: HTMLElement,
  options: PresenceMapMountOptions,
): PresenceMapController {
  rootEl.replaceChildren();

  const visibleIds = new Set(
    options.participants.map((p) => p.participant_id),
  );
  let participants = [...options.participants];
  const includeParticipantIdInTooltip =
    options.includeParticipantIdInTooltip === true;

  const filtersEl = document.createElement("div");
  filtersEl.className = "rules-presence-map-filters";
  if (options.showParticipantFilters) {
    for (const participant of options.participants) {
      const color = participantMarkerColor(
        participant.tracking_device_label,
        participant.participant_id,
      );
      const label = document.createElement("label");
      label.className = "rules-presence-map-filter";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.dataset.participantId = participant.participant_id;
      const swatch = document.createElement("span");
      swatch.className = "rules-presence-map-filter-swatch";
      swatch.style.backgroundColor = color;
      const name = document.createElement("span");
      name.textContent = participant.display_name;
      label.append(checkbox, swatch, name);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          visibleIds.add(participant.participant_id);
        } else {
          visibleIds.delete(participant.participant_id);
        }
        syncParticipantVisibility();
        renderPresenceMapLegend(legendEl, participants, visibleIds);
        fitVisibleBounds();
      });
      filtersEl.append(label);
    }
  } else {
    filtersEl.hidden = true;
  }

  const shellEl = document.createElement("div");
  shellEl.className = "rules-presence-map-shell";
  const mapEl = document.createElement("div");
  mapEl.className = "rules-presence-map";
  const legendEl = document.createElement("div");
  legendEl.className = "rules-presence-map-legend";
  legendEl.hidden = true;
  shellEl.append(mapEl, legendEl);
  rootEl.append(filtersEl, shellEl);

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
      layers.marker.setStyle({ fillOpacity: show ? 0.9 : 0, opacity: show ? 1 : 0 });
      if (layers.accuracy !== null) {
        layers.accuracy.setStyle({ fillOpacity: show ? 0.08 : 0, opacity: show ? 1 : 0 });
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

    const visibleWithFix = participants.filter(
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

  const removeParticipantLayer = (participantId: string): void => {
    const layers = participantLayerById.get(participantId);
    if (layers === undefined) {
      return;
    }
    map.removeLayer(layers.marker);
    if (layers.accuracy !== null) {
      map.removeLayer(layers.accuracy);
    }
    participantLayerById.delete(participantId);
  };

  const upsertParticipantLayer = (participant: PresenceMapParticipant): void => {
    const fix = participant.last_fix;
    if (fix === null) {
      removeParticipantLayer(participant.participant_id);
      return;
    }
    const color = participantMarkerColor(
      participant.tracking_device_label,
      participant.participant_id,
    );
    const tooltipHtml = formatParticipantTooltipHtml(participant, {
      includeParticipantId: includeParticipantIdInTooltip,
    });
    const tooltipOptions: L.TooltipOptions = {
      className: "rules-presence-map-tooltip",
      direction: "auto",
      offset: [0, -12],
      opacity: 1,
      sticky: true,
    };
    const existing = participantLayerById.get(participant.participant_id);
    if (existing !== undefined) {
      existing.marker.setLatLng([fix.lat, fix.lon]);
      existing.marker.setStyle(participantMarkerOptions(color));
      existing.marker.setTooltipContent(tooltipHtml);
      if (fix.accuracy_m !== null && fix.accuracy_m > 0) {
        if (existing.accuracy === null) {
          existing.accuracy = L.circle([fix.lat, fix.lon], {
            color: "var(--pending)",
            dashArray: "4 4",
            fillColor: "var(--pending)",
            fillOpacity: 0.08,
            interactive: false,
            radius: fix.accuracy_m,
            weight: 1,
          }).addTo(map);
        } else {
          existing.accuracy.setLatLng([fix.lat, fix.lon]);
          existing.accuracy.setRadius(fix.accuracy_m);
        }
      } else if (existing.accuracy !== null) {
        map.removeLayer(existing.accuracy);
        existing.accuracy = null;
      }
      return;
    }
    const marker = L.circleMarker(
      [fix.lat, fix.lon],
      participantMarkerOptions(color),
    )
      .bindTooltip(tooltipHtml, tooltipOptions)
      .addTo(map);
    let accuracy: L.Circle | null = null;
    if (fix.accuracy_m !== null && fix.accuracy_m > 0) {
      accuracy = L.circle([fix.lat, fix.lon], {
        color: "var(--pending)",
        dashArray: "4 4",
        fillColor: "var(--pending)",
        fillOpacity: 0.08,
        interactive: false,
        radius: fix.accuracy_m,
        weight: 1,
      }).addTo(map);
    }
    participantLayerById.set(participant.participant_id, { accuracy, marker });
  };

  const applyParticipants = (nextParticipants: PresenceMapParticipant[]): void => {
    participants = [...nextParticipants];
    const nextIds = new Set(participants.map((participant) => participant.participant_id));
    for (const participantId of [...participantLayerById.keys()]) {
      if (!nextIds.has(participantId)) {
        removeParticipantLayer(participantId);
      }
    }
    for (const participant of participants) {
      upsertParticipantLayer(participant);
    }
    syncParticipantVisibility();
    renderPresenceMapLegend(legendEl, participants, visibleIds);
  };

  applyParticipants(options.participants);
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
    updateParticipants(nextParticipants: PresenceMapParticipant[]): void {
      applyParticipants(nextParticipants);
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
