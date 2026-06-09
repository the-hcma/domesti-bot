// Shared astronomical condition windows and status helpers.

import type { RulesSunOut } from "./types.js";

export type AstronomicalWindowBoundary = "midnight";

export const AFTER_SUNSET_WINDOW_DESCRIPTION =
  "Default window: sunset to midnight (midnight boundary will be configurable per rule).";
export const BEFORE_SUNRISE_WINDOW_DESCRIPTION =
  "Default window: midnight to sunrise (midnight boundary will be configurable per rule).";

export function localMinutesFromIso(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

export function localMinutesNow(): number {
  const now = new Date();
  return now.getHours() * 60 + now.getMinutes();
}

export function isInAfterSunsetWindow(
  sunsetAtIso: string,
  offsetMinutes: number,
): boolean {
  const now = localMinutesNow();
  const sunset = localMinutesFromIso(sunsetAtIso) + offsetMinutes;
  return now >= sunset;
}

export function isInBeforeSunriseWindow(
  sunriseAtIso: string,
  offsetMinutes: number,
): boolean {
  const now = localMinutesNow();
  const sunrise = localMinutesFromIso(sunriseAtIso) + offsetMinutes;
  return now < sunrise;
}

export function afterSunsetStatusMessage(sun: RulesSunOut): {
  dynamicLabel: string;
  primary: string;
} {
  const inWindow = isInAfterSunsetWindow(sun.sunset_at, 0);
  if (inWindow) {
    return {
      dynamicLabel: "After sunset (dynamic)",
      primary: `Evening window active — sunset was ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  if (!sun.is_dark) {
    return {
      dynamicLabel: "After sunset (dynamic)",
      primary: `Light until sunset at ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  return {
    dynamicLabel: "After sunset (dynamic)",
    primary: `Outside evening window — next sunset at ${formatLocalTime(sun.sunset_at)}`,
  };
}

export function beforeSunriseStatusMessage(sun: RulesSunOut): {
  dynamicLabel: string;
  primary: string;
} {
  const inWindow = isInBeforeSunriseWindow(sun.sunrise_at, 0);
  if (inWindow) {
    return {
      dynamicLabel: "Before sunrise (dynamic)",
      primary: `Morning window active — sunrise at ${formatLocalTime(sun.sunrise_at)}`,
    };
  }
  if (!sun.is_dark) {
    return {
      dynamicLabel: "Before sunrise (dynamic)",
      primary: `Daytime — next sunrise at ${formatLocalTime(sun.sunrise_at)}`,
    };
  }
  return {
    dynamicLabel: "Before sunrise (dynamic)",
    primary: `Outside morning window — sunrise was ${formatLocalTime(sun.sunrise_at)}`,
  };
}

function formatLocalTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}
