// Unified Leaflet map for user locations + geofence overlays.

import L from "leaflet";
import { formatLocalTimestamp, formatUtcTimestampTitle } from "./format-timestamp.js";
import { userMarkerColor } from "./map-device-colors.js";
import { haversineM } from "./rules-mock-fixtures.js";
import { DEFAULT_MIN_LOCATION_ACCURACY_M } from "./rules-constants.js";
import { userDisplayLabel } from "./rules-ui-helpers.js";
import type { GeofenceOut, UserLocationOut, UserStatusOut } from "./types.js";

/** Extra meters beyond a geofence radius to still show a user on the geofence map. */
export const NEARBY_GEOFENCE_BUFFER_M = 500;

/** ~Street-block scale when a single user is selected. */
const SINGLE_USER_BOUNDS_RADIUS_M = 220;

const SINGLE_USER_MAX_ZOOM = 17;
const MULTI_USER_MAX_ZOOM = 15;

export interface PresenceMapUser {
  age_seconds: number | null;
  display_name: string;
  inside_geofence_ids: string[];
  last_location: UserLocationOut | null;
  user_id: string;
  tracking_device_label: string;
}

export interface PresenceMapMountOptions {
  geofences: GeofenceOut[];
  /** When true, tooltip title includes ``user_id`` (Users tab). */
  includeUserIdInTooltip?: boolean;
  users: PresenceMapUser[];
  showUserFilters: boolean;
}

export interface PresenceMapController {
  destroy(): void;
  updateUsers(users: PresenceMapUser[]): void;
}

interface UserLayers {
  accuracy: L.Circle | null;
  marker: L.CircleMarker;
}

interface ShellTooltipController {
  attach(marker: L.CircleMarker, getTooltipHtml: () => string): void;
  detach(marker: L.CircleMarker): void;
  destroy(): void;
  refresh(): void;
}

function isUserMarkerVisible(marker: L.CircleMarker): boolean {
  return (marker.options.fillOpacity ?? 1) > 0 && (marker.options.opacity ?? 1) > 0;
}

function userMarkerAtClientPoint(
  map: L.Map,
  markers: Iterable<L.CircleMarker>,
  clientX: number,
  clientY: number,
): L.CircleMarker | null {
  const mapRect = map.getContainer().getBoundingClientRect();
  if (
    clientX < mapRect.left
    || clientX > mapRect.right
    || clientY < mapRect.top
    || clientY > mapRect.bottom
  ) {
    return null;
  }
  const containerPoint = L.point(clientX - mapRect.left, clientY - mapRect.top);
  const layerPoint = map.containerPointToLayerPoint(containerPoint);
  let hit: L.CircleMarker | null = null;
  let hitDistance = Infinity;
  for (const marker of markers) {
    if (!isUserMarkerVisible(marker)) {
      continue;
    }
    const center = map.latLngToLayerPoint(marker.getLatLng());
    const distance = layerPoint.distanceTo(center);
    const radius = marker.getRadius() + (marker.options.weight ?? 1);
    if (distance <= radius && distance < hitDistance) {
      hit = marker;
      hitDistance = distance;
    }
  }
  return hit;
}

export function createShellTooltipController(
  shellEl: HTMLElement,
  map: L.Map,
): ShellTooltipController {
  const tooltipEl = document.createElement("div");
  tooltipEl.className = "rules-presence-map-tooltip rules-presence-map-hover-tooltip";
  shellEl.append(tooltipEl);

  const tooltipHtmlByMarker = new Map<L.CircleMarker, () => string>();
  let activeMarker: L.CircleMarker | null = null;
  let lastPointer: { clientX: number; clientY: number } | null = null;

  const hide = (): void => {
    activeMarker = null;
    tooltipEl.classList.remove("is-visible");
    tooltipEl.style.transform = "";
    tooltipEl.replaceChildren();
  };

  const positionAtPointer = (
    clientX: number,
    clientY: number,
    getTooltipHtml: () => string,
  ): void => {
    const shellRect = shellEl.getBoundingClientRect();
    if (
      clientX < shellRect.left
      || clientX > shellRect.right
      || clientY < shellRect.top
      || clientY > shellRect.bottom
    ) {
      hide();
      return;
    }

    tooltipEl.classList.remove("is-visible");
    tooltipEl.innerHTML = getTooltipHtml();

    const margin = 8;
    const pointerX = clientX - shellRect.left;
    const pointerY = clientY - shellRect.top;
    tooltipEl.style.transform =
      `translate(${pointerX}px, ${pointerY - 12}px) translate(-50%, -100%)`;

    let tRect = tooltipEl.getBoundingClientRect();
    if (tRect.top < shellRect.top + margin) {
      tooltipEl.style.transform =
        `translate(${pointerX}px, ${pointerY + 16}px) translate(-50%, 0)`;
      tRect = tooltipEl.getBoundingClientRect();
    }

    let shiftX = 0;
    if (tRect.left < shellRect.left + margin) {
      shiftX = shellRect.left + margin - tRect.left;
    } else if (tRect.right > shellRect.right - margin) {
      shiftX = shellRect.right - margin - tRect.right;
    }
    if (shiftX !== 0) {
      const current = tooltipEl.style.transform;
      tooltipEl.style.transform = `${current} translateX(${shiftX}px)`;
    }

    tooltipEl.classList.add("is-visible");
  };

  const updateForPointer = (clientX: number, clientY: number): void => {
    const marker = userMarkerAtClientPoint(
      map,
      tooltipHtmlByMarker.keys(),
      clientX,
      clientY,
    );
    if (marker === null) {
      hide();
      return;
    }
    const getTooltipHtml = tooltipHtmlByMarker.get(marker);
    if (getTooltipHtml === undefined) {
      hide();
      return;
    }
    activeMarker = marker;
    positionAtPointer(clientX, clientY, getTooltipHtml);
  };

  const onMapMouseMove = (event: L.LeafletMouseEvent): void => {
    lastPointer = {
      clientX: event.originalEvent.clientX,
      clientY: event.originalEvent.clientY,
    };
    updateForPointer(lastPointer.clientX, lastPointer.clientY);
  };

  const onMapMouseOut = (): void => {
    lastPointer = null;
    hide();
  };

  const onMapInteractionStart = (): void => {
    lastPointer = null;
    hide();
  };

  map.on("mousemove", onMapMouseMove);
  map.on("mouseout", onMapMouseOut);
  map.on("movestart", onMapInteractionStart);
  map.on("zoomstart", onMapInteractionStart);
  shellEl.addEventListener("mouseleave", onMapMouseOut);

  return {
    attach(marker, getTooltipHtml) {
      tooltipHtmlByMarker.set(marker, getTooltipHtml);
    },
    detach(marker) {
      tooltipHtmlByMarker.delete(marker);
      if (activeMarker === marker) {
        hide();
      }
    },
    destroy() {
      map.off("mousemove", onMapMouseMove);
      map.off("mouseout", onMapMouseOut);
      map.off("movestart", onMapInteractionStart);
      map.off("zoomstart", onMapInteractionStart);
      shellEl.removeEventListener("mouseleave", onMapMouseOut);
      hide();
      tooltipEl.remove();
    },
    refresh() {
      if (lastPointer === null) {
        hide();
        return;
      }
      updateForPointer(lastPointer.clientX, lastPointer.clientY);
    },
  };
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

export function geofenceLabelsForIds(
  geofenceIds: readonly string[],
  geofences: readonly GeofenceOut[],
): string[] {
  const labelById = new Map(geofences.map((geofence) => [geofence.geofence_id, geofence.label]));
  return geofenceIds.map((geofenceId) => labelById.get(geofenceId) ?? geofenceId);
}

export function formatInsideGeofencesLine(
  geofenceIds: readonly string[],
  geofences: readonly GeofenceOut[],
): string {
  if (geofenceIds.length === 0) {
    return "Outside all geofences";
  }
  return `Inside ${geofenceLabelsForIds(geofenceIds, geofences).join(", ")}`;
}

export function formatUserTooltipHtml(
  user: PresenceMapUser,
  options?: {
    geofences?: readonly GeofenceOut[];
    includeUserId?: boolean;
  },
): string {
  const title = options?.includeUserId === true
    ? `${user.display_name} (${user.user_id})`
    : user.display_name;
  const lines: string[] = [`<strong>${escapeHtml(title)}</strong>`];
  lines.push(`Tracking device: ${escapeHtml(user.tracking_device_label)}`);
  const inside = formatInsideGeofencesLine(
    user.inside_geofence_ids,
    options?.geofences ?? [],
  );
  lines.push(`${formatAge(user.age_seconds)} · ${escapeHtml(inside)}`);
  const location = user.last_location;
  if (location !== null) {
    const accuracy = location.accuracy_m === null ? "unknown" : `±${location.accuracy_m} m`;
    lines.push(
      `${location.lat.toFixed(5)}, ${location.lon.toFixed(5)} · ${escapeHtml(accuracy)}`,
    );
    lines.push(
      `Location at <span title="${escapeHtml(formatUtcTimestampTitle(location.received_at))}">`
        + `${escapeHtml(formatLocalTimestamp(location.received_at))}</span>`,
    );
    if (
      location.accuracy_m !== null
      && location.accuracy_m > DEFAULT_MIN_LOCATION_ACCURACY_M
    ) {
      lines.push(
        `Low accuracy — ignored by rules (&gt;${DEFAULT_MIN_LOCATION_ACCURACY_M} m)`,
      );
    }
  } else {
    lines.push("No location yet");
  }
  return lines
    .map((line) => `<span class="rules-presence-map-tooltip-line">${line}</span>`)
    .join("");
}

export function userNearEnabledGeofence(
  userLocation: UserLocationOut,
  geofences: GeofenceOut[],
): boolean {
  for (const geofence of geofences) {
    if (!geofence.enabled) {
      continue;
    }
    const dist = haversineM(
      userLocation.lat,
      userLocation.lon,
      geofence.center_lat,
      geofence.center_lon,
    );
    if (dist <= geofence.radius_m + NEARBY_GEOFENCE_BUFFER_M) {
      return true;
    }
  }
  return false;
}

function userMarkerOptions(color: string): L.CircleMarkerOptions {
  return {
    className: "rules-presence-user-marker",
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
  users: PresenceMapUser[],
  visibleIds: ReadonlySet<string>,
): void {
  legendEl.replaceChildren();
  const withLocation = users.filter((user) => user.last_location !== null);
  if (withLocation.length < 2) {
    legendEl.hidden = true;
    return;
  }
  legendEl.hidden = false;

  const title = document.createElement("div");
  title.className = "rules-presence-map-legend-title";
  title.textContent = "Users";
  legendEl.append(title);

  const sorted = [...withLocation].sort((left, right) =>
    left.display_name.localeCompare(right.display_name, undefined, {
      sensitivity: "base",
    }),
  );
  for (const user of sorted) {
    const color = userMarkerColor(
      user.tracking_device_label,
      user.user_id,
    );
    const item = document.createElement("div");
    item.className = "rules-presence-map-legend-item";
    if (!visibleIds.has(user.user_id)) {
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
    name.textContent = user.display_name;
    const device = document.createElement("span");
    device.className = "rules-presence-map-legend-device";
    device.textContent = user.tracking_device_label;
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
    options.users.map((p) => p.user_id),
  );
  let users = [...options.users];
  const includeUserIdInTooltip =
    options.includeUserIdInTooltip === true;

  const filtersEl = document.createElement("div");
  filtersEl.className = "rules-presence-map-filters";
  if (options.showUserFilters) {
    for (const user of options.users) {
      const color = userMarkerColor(
        user.tracking_device_label,
        user.user_id,
      );
      const label = document.createElement("label");
      label.className = "rules-presence-map-filter";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.dataset.userId = user.user_id;
      const swatch = document.createElement("span");
      swatch.className = "rules-presence-map-filter-swatch";
      swatch.style.backgroundColor = color;
      const name = document.createElement("span");
      name.textContent = user.display_name;
      label.append(checkbox, swatch, name);
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) {
          visibleIds.add(user.user_id);
        } else {
          visibleIds.delete(user.user_id);
        }
        syncUserVisibility();
        renderPresenceMapLegend(legendEl, users, visibleIds);
        shellTooltip.refresh();
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

  const tooltipHtmlByUserId = new Map<string, string>();

  const map = L.map(mapEl, {
    attributionControl: false,
    scrollWheelZoom: true,
    zoomControl: true,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(map);

  const shellTooltip = createShellTooltipController(shellEl, map);

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

  const userLayerById = new Map<string, UserLayers>();

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

  const syncUserVisibility = (): void => {
    for (const [userId, layers] of userLayerById) {
      const show = visibleIds.has(userId);
      layers.marker.setStyle({ fillOpacity: show ? 0.9 : 0, opacity: show ? 1 : 0 });
      if (layers.accuracy !== null) {
        layers.accuracy.setStyle({ fillOpacity: show ? 0.08 : 0, opacity: show ? 1 : 0 });
      }
    }
  };

  const fitVisibleBounds = (): void => {
    const boundsLayers: L.Layer[] = [...geofenceLayers];
    for (const [userId, layers] of userLayerById) {
      if (!visibleIds.has(userId)) {
        continue;
      }
      boundsLayers.push(layers.marker);
      if (layers.accuracy !== null) {
        boundsLayers.push(layers.accuracy);
      }
    }

    const visibleWithLocation = users.filter(
      (p) => visibleIds.has(p.user_id) && p.last_location !== null,
    );

    if (visibleWithLocation.length === 1) {
      const reading = visibleWithLocation[0]?.last_location;
      if (reading !== undefined && reading !== null) {
        const center = L.latLng(reading.lat, reading.lon);
        map.fitBounds(center.toBounds(SINGLE_USER_BOUNDS_RADIUS_M), {
          maxZoom: SINGLE_USER_MAX_ZOOM,
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
          maxZoom: MULTI_USER_MAX_ZOOM,
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

  const removeUserLayer = (userId: string): void => {
    const layers = userLayerById.get(userId);
    if (layers === undefined) {
      return;
    }
    shellTooltip.detach(layers.marker);
    tooltipHtmlByUserId.delete(userId);
    map.removeLayer(layers.marker);
    if (layers.accuracy !== null) {
      map.removeLayer(layers.accuracy);
    }
    userLayerById.delete(userId);
  };

  const upsertUserLayer = (user: PresenceMapUser): void => {
    const location = user.last_location;
    if (location === null) {
      removeUserLayer(user.user_id);
      return;
    }
    const color = userMarkerColor(
      user.tracking_device_label,
      user.user_id,
    );
    const tooltipHtml = formatUserTooltipHtml(user, {
      geofences: options.geofences,
      includeUserId: includeUserIdInTooltip,
    });
    tooltipHtmlByUserId.set(user.user_id, tooltipHtml);
    const existing = userLayerById.get(user.user_id);
    if (existing !== undefined) {
      existing.marker.setLatLng([location.lat, location.lon]);
      existing.marker.setStyle(userMarkerOptions(color));
      if (location.accuracy_m !== null && location.accuracy_m > 0) {
        if (existing.accuracy === null) {
          existing.accuracy = L.circle([location.lat, location.lon], {
            color: "var(--pending)",
            dashArray: "4 4",
            fillColor: "var(--pending)",
            fillOpacity: 0.08,
            interactive: false,
            radius: location.accuracy_m,
            weight: 1,
          }).addTo(map);
        } else {
          existing.accuracy.setLatLng([location.lat, location.lon]);
          existing.accuracy.setRadius(location.accuracy_m);
        }
      } else if (existing.accuracy !== null) {
        map.removeLayer(existing.accuracy);
        existing.accuracy = null;
      }
      return;
    }
    const marker = L.circleMarker(
      [location.lat, location.lon],
      userMarkerOptions(color),
    ).addTo(map);
    const userId = user.user_id;
    shellTooltip.attach(marker, () => tooltipHtmlByUserId.get(userId) ?? "");
    let accuracy: L.Circle | null = null;
    if (location.accuracy_m !== null && location.accuracy_m > 0) {
      accuracy = L.circle([location.lat, location.lon], {
        color: "var(--pending)",
        dashArray: "4 4",
        fillColor: "var(--pending)",
        fillOpacity: 0.08,
        interactive: false,
        radius: location.accuracy_m,
        weight: 1,
      }).addTo(map);
    }
    userLayerById.set(user.user_id, { accuracy, marker });
  };

  const applyUsers = (nextUsers: PresenceMapUser[]): void => {
    users = [...nextUsers];
    const nextIds = new Set(users.map((u) => u.user_id));
    for (const userId of [...userLayerById.keys()]) {
      if (!nextIds.has(userId)) {
        removeUserLayer(userId);
      }
    }
    for (const user of users) {
      upsertUserLayer(user);
    }
    syncUserVisibility();
    renderPresenceMapLegend(legendEl, users, visibleIds);
    shellTooltip.refresh();
  };

  applyUsers(options.users);

  const refreshMapLayout = (): void => {
    map.invalidateSize();
    fitVisibleBounds();
  };

  refreshMapLayout();
  window.requestAnimationFrame(() => {
    refreshMapLayout();
    window.requestAnimationFrame(refreshMapLayout);
  });

  return {
    destroy(): void {
      shellTooltip.destroy();
      map.remove();
      rootEl.replaceChildren();
    },
    updateUsers(nextUsers: PresenceMapUser[]): void {
      applyUsers(nextUsers);
    },
  };
}

export function userStatusToMapUser(
  user: UserStatusOut,
): PresenceMapUser {
  return {
    age_seconds: user.age_seconds,
    display_name: userDisplayLabel(
      user.user_id,
      user.display_name,
    ),
    inside_geofence_ids: user.inside_geofence_ids,
    last_location: user.last_location,
    user_id: user.user_id,
    tracking_device_label: user.tracking_device_label,
  };
}

export function renderUserDetailText(
  user: PresenceMapUser,
  includeUserId: boolean,
  geofences: readonly GeofenceOut[] = [],
): HTMLElement {
  const card = document.createElement("article");
  card.className = "rules-card rules-user-detail-card";
  const name = document.createElement("strong");
  name.textContent = includeUserId
    ? `${user.display_name} (${user.user_id})`
    : user.display_name;
  const deviceMeta = document.createElement("p");
  deviceMeta.className = "rules-card-meta";
  deviceMeta.textContent = `Tracking device: ${user.tracking_device_label}`;
  const meta = document.createElement("p");
  meta.className = "rules-card-meta";
  meta.textContent = `${formatAge(user.age_seconds)} · ${formatInsideGeofencesLine(
    user.inside_geofence_ids,
    geofences,
  )}`;
  card.append(name, deviceMeta, meta);
  const location = user.last_location;
  if (location !== null) {
    const coords = document.createElement("p");
    coords.className = "rules-card-meta";
    const accuracy = location.accuracy_m === null ? "unknown" : `±${location.accuracy_m} m`;
    coords.textContent = `${location.lat.toFixed(5)}, ${location.lon.toFixed(5)} · ${accuracy}`;
    card.append(coords);
    if (
      location.accuracy_m !== null
      && location.accuracy_m > DEFAULT_MIN_LOCATION_ACCURACY_M
    ) {
      const warn = document.createElement("p");
      warn.className = "rules-card-warn";
      warn.textContent = `Low accuracy — ignored by rules (>${DEFAULT_MIN_LOCATION_ACCURACY_M} m)`;
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
