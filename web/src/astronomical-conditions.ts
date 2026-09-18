// Shared astronomical condition windows and status helpers.

import type { RulesSunOut } from "./types.js";

export { AstronomicalWindowBoundary } from "./closed-sets.js";

export const MINUTES_PER_DAY = 24 * 60;

export const AFTER_SUNSET_WINDOW_DESCRIPTION =
  "Evening window: local sunset through midnight.";
export const BEFORE_SUNRISE_WINDOW_DESCRIPTION =
  "Morning window: local midnight through sunrise.";
export const BEFORE_SUNSET_WINDOW_DESCRIPTION =
  "Daytime window: local midnight through sunset.";
export const DAYLIGHT_WINDOW_DESCRIPTION =
  "Daylight window: local sunrise through sunset.";

export function localMinutesFromIso(iso: string): number {
  const d = new Date(iso);
  return d.getHours() * 60 + d.getMinutes();
}

export function localMinutesNow(): number {
  const now = new Date();
  return now.getHours() * 60 + now.getMinutes();
}

export function isInAfterSunsetWindowAt(
  nowMinutes: number,
  sunsetMinutes: number,
  offsetMinutes: number,
): boolean {
  const start = sunsetMinutes + offsetMinutes;
  if (start >= MINUTES_PER_DAY) {
    return false;
  }
  return nowMinutes >= start && nowMinutes < MINUTES_PER_DAY;
}

export function isInBeforeSunriseWindowAt(
  nowMinutes: number,
  sunriseMinutes: number,
  offsetMinutes: number,
): boolean {
  return isInMidnightToAnchorWindowAt(nowMinutes, sunriseMinutes + offsetMinutes);
}

export function isInBeforeSunsetWindowAt(
  nowMinutes: number,
  sunsetMinutes: number,
  offsetMinutes: number,
): boolean {
  return isInMidnightToAnchorWindowAt(nowMinutes, sunsetMinutes + offsetMinutes);
}

// Shared body for isInBeforeSunriseWindowAt / isInBeforeSunsetWindowAt: midnight through anchorMinutes.
function isInMidnightToAnchorWindowAt(nowMinutes: number, anchorMinutes: number): boolean {
  return nowMinutes >= 0 && nowMinutes < anchorMinutes;
}

export function isInAfterSunsetWindow(
  sunsetAtIso: string,
  offsetMinutes: number,
): boolean {
  return isInAfterSunsetWindowAt(
    localMinutesNow(),
    localMinutesFromIso(sunsetAtIso),
    offsetMinutes,
  );
}

export function isInBeforeSunriseWindow(
  sunriseAtIso: string,
  offsetMinutes: number,
): boolean {
  return isInBeforeSunriseWindowAt(
    localMinutesNow(),
    localMinutesFromIso(sunriseAtIso),
    offsetMinutes,
  );
}

export function isInBeforeSunsetWindow(
  sunsetAtIso: string,
  offsetMinutes: number,
): boolean {
  return isInBeforeSunsetWindowAt(
    localMinutesNow(),
    localMinutesFromIso(sunsetAtIso),
    offsetMinutes,
  );
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

export function beforeSunsetStatusMessage(sun: RulesSunOut): {
  dynamicLabel: string;
  primary: string;
} {
  const inWindow = isInBeforeSunsetWindow(sun.sunset_at, 0);
  if (inWindow && !sun.is_dark) {
    return {
      dynamicLabel: "Before sunset (dynamic)",
      primary: `Daytime window active — sunset at ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  if (inWindow) {
    return {
      dynamicLabel: "Before sunset (dynamic)",
      primary: `Overnight — window active until sunset at ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  return {
    dynamicLabel: "Before sunset (dynamic)",
    primary: `Outside daytime window — sunset was ${formatLocalTime(sun.sunset_at)}`,
  };
}

export function daylightStatusMessage(sun: RulesSunOut): {
  dynamicLabel: string;
  primary: string;
} {
  if (!sun.is_dark) {
    return {
      dynamicLabel: "Daylight (dynamic)",
      primary: `Daylight active — sunrise ${formatLocalTime(sun.sunrise_at)}, sunset ${formatLocalTime(sun.sunset_at)}`,
    };
  }
  return {
    dynamicLabel: "Daylight (dynamic)",
    primary: `Outside daylight — sunrise ${formatLocalTime(sun.sunrise_at)}, sunset ${formatLocalTime(sun.sunset_at)}`,
  };
}

function formatLocalTime(iso: string): string {
  return new Date(iso).toLocaleTimeString([], {
    hour: "numeric",
    minute: "2-digit",
  });
}
