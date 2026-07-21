// Compact Leaflet inset for the Automations → Users home lat/lon card.

import L from "leaflet";

const CONFIGURED_ZOOM = 15;
const UNCONFIGURED_ZOOM = 1;
const UNCONFIGURED_CENTER: L.LatLngExpression = [20, 0];

export interface HomeLocationMapInset {
  destroy(): void;
  invalidateSize(): void;
  setLocation(lat: number, lon: number, label: string | null): void;
}

export interface HomeLocationMapInsetOptions {
  label: string | null;
  lat: number;
  lon: number;
}

function homeIsConfigured(lat: number, lon: number): boolean {
  return !(lat === 0 && lon === 0) && Number.isFinite(lat) && Number.isFinite(lon);
}

function homeDivIcon(): L.DivIcon {
  return L.divIcon({
    className: "rules-home-location-map-pin",
    html: "\u{1f3e0}",
    iconAnchor: [10, 10],
    iconSize: [20, 20],
  });
}

/** Mount a small read-only peek map for the configured home point. */
export function mountHomeLocationMapInset(
  container: HTMLElement,
  options: HomeLocationMapInsetOptions,
): HomeLocationMapInset {
  const map = L.map(container, {
    attributionControl: false,
    zoomControl: false,
  });
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
  }).addTo(map);

  let marker: L.Marker | null = null;
  let viewLat = options.lat;
  let viewLon = options.lon;

  const recenterAfterSize = (): void => {
    map.invalidateSize({ animate: false });
    if (homeIsConfigured(viewLat, viewLon)) {
      map.setView([viewLat, viewLon], CONFIGURED_ZOOM, { animate: false });
    } else {
      map.setView(UNCONFIGURED_CENTER, UNCONFIGURED_ZOOM, { animate: false });
    }
  };

  const apply = (lat: number, lon: number, label: string | null): void => {
    viewLat = lat;
    viewLon = lon;
    if (marker !== null) {
      map.removeLayer(marker);
      marker = null;
    }
    if (!homeIsConfigured(lat, lon)) {
      map.setView(UNCONFIGURED_CENTER, UNCONFIGURED_ZOOM, { animate: false });
      container.classList.add("rules-home-location-map-empty");
      return;
    }
    container.classList.remove("rules-home-location-map-empty");
    const tip = label !== null && label.trim() !== "" ? label.trim() : "Home";
    marker = L.marker([lat, lon], { icon: homeDivIcon() })
      .bindTooltip(tip, { permanent: false })
      .addTo(map);
    map.setView([lat, lon], CONFIGURED_ZOOM, { animate: false });
  };

  apply(options.lat, options.lon, options.label);

  // Dialog layout / grid columns often settle after first paint; Leaflet’s first
  // setView frequently ran at a zero-size container. Re-center whenever the
  // host gains a real size and after a couple of animation frames.
  let destroyed = false;
  const resizeObserver = new ResizeObserver(() => {
    if (destroyed) {
      return;
    }
    if (container.clientWidth > 0 && container.clientHeight > 0) {
      recenterAfterSize();
    }
  });
  resizeObserver.observe(container);
  requestAnimationFrame(() => {
    if (destroyed) {
      return;
    }
    recenterAfterSize();
    requestAnimationFrame(() => {
      if (destroyed) {
        return;
      }
      recenterAfterSize();
    });
  });

  return {
    destroy(): void {
      destroyed = true;
      resizeObserver.disconnect();
      map.remove();
    },
    invalidateSize(): void {
      if (destroyed) {
        return;
      }
      recenterAfterSize();
    },
    setLocation(lat: number, lon: number, label: string | null): void {
      if (destroyed) {
        return;
      }
      apply(lat, lon, label);
      recenterAfterSize();
    },
  };
}
