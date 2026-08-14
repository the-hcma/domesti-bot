/**
 * Native ``title`` identity lines for dashboard tiles and EP1 header sensors.
 *
 * Desktop hover (not phone) surfaces MAC / IP / extra identity the same way
 * on both the device tiles and the header climate / occupancy controls.
 */

export const DEVICE_PROPERTIES_MENU_HINT =
  "Right-click to edit device properties";

/** Fields needed to format a device-identity hover tooltip. */
export interface DeviceIdentityTooltipSource {
  host?: string | null;
  identity_details?: readonly string[];
  label?: string;
  mac_address: string;
}

export interface DeviceIdentityTooltipOptions {
  includeLabel?: boolean;
  includePropertiesHint?: boolean;
}

export function formatDeviceIdentityTooltip(
  device: DeviceIdentityTooltipSource,
  options?: DeviceIdentityTooltipOptions,
): string {
  const lines: string[] = [];
  if (options?.includePropertiesHint === true) {
    lines.push(DEVICE_PROPERTIES_MENU_HINT, "");
  }
  if (options?.includeLabel === true) {
    const label = (device.label ?? "").trim();
    if (label !== "") {
      lines.push(label);
    }
  }
  lines.push(`MAC address: ${device.mac_address}`);
  const host = (device.host ?? "").trim();
  if (host !== "") {
    lines.push(`IP: ${host}`);
  }
  for (const detail of device.identity_details ?? []) {
    const text = detail.trim();
    if (text !== "") {
      lines.push(text);
    }
  }
  return lines.join("\n");
}

export function formatDeviceIdentityTooltipLabel(
  device: DeviceIdentityTooltipSource,
  stateSuffix: string | null = null,
): string {
  const base = (device.label ?? "").trim() || device.mac_address;
  if (stateSuffix == null || stateSuffix.trim() === "") {
    return base;
  }
  return `${base} (${stateSuffix})`;
}
