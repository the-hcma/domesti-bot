// Stable map marker colors for participants and tracking devices.

/** Ordered for maximum visual difference between adjacent colors. */
export const MAP_DEVICE_MARKER_COLORS = [
  "#c82333",
  "#0056b3",
  "#28a745",
  "#e65100",
  "#6f42c1",
  "#00bcd4",
  "#d63384",
  "#795548",
  "#00695c",
  "#ff9800",
] as const;

export function hashString(value: string): number {
  let hash = 0;
  for (let i = 0; i < value.length; i++) {
    const char = value.charCodeAt(i);
    hash = (hash << 5) - hash + char;
    hash = hash & hash;
  }
  return Math.abs(hash);
}

export function selectStablePaletteColor(
  key: string,
  palette: readonly string[],
): string {
  if (palette.length === 0) {
    throw new Error("Expected a non-empty palette, got empty sequence");
  }
  const picked = palette.at(hashString(key) % palette.length);
  if (picked === undefined) {
    throw new Error("Expected a non-empty palette, got empty sequence");
  }
  return picked;
}

/** Device label when set; otherwise participant id. */
export function participantMarkerColorKey(
  trackingDeviceLabel: string,
  participantId: string,
): string {
  const device = trackingDeviceLabel.trim();
  return device !== "" ? device : participantId;
}

export function participantMarkerColor(
  trackingDeviceLabel: string,
  participantId: string,
): string {
  return selectStablePaletteColor(
    participantMarkerColorKey(trackingDeviceLabel, participantId),
    MAP_DEVICE_MARKER_COLORS,
  );
}
