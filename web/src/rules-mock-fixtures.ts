// Seed fixtures for the motivating Henrique + Kristen arrival rule scenario.
// Coordinates match ``tests/python/test_rule_engine.py``.

import type {
  GeofenceOut,
  ParticipantFixOut,
  ParticipantOut,
  RuleActionDeviceOut,
  RuleOut,
  SettingsLocationOut,
} from "./types.js";

/** House geofence center (41.194072, -73.888325) — 250 m radius. */
export const MOCK_HOUSE_CENTER_LAT = 41.194072;
export const MOCK_HOUSE_CENTER_LON = -73.8883254;
export const MOCK_HOUSE_RADIUS_M = 250;

export const MOCK_HENRIQUE_AT_HOME_LAT = 41.194085;
export const MOCK_HENRIQUE_AT_HOME_LON = -73.888365;

export const MOCK_KRISTEN_OUTSIDE_LAT = 44.417597;
export const MOCK_KRISTEN_OUTSIDE_LON = -72.023842;

export interface MockStoreSeed {
  geofences: GeofenceOut[];
  participants: ParticipantOut[];
  participant_fixes: Record<string, ParticipantFixOut>;
  rules: RuleOut[];
  rule_last_fired_at: Record<string, string | null>;
  settings_location: SettingsLocationOut;
  action_devices: RuleActionDeviceOut[];
}

function isoMinutesAgo(minutes: number): string {
  return new Date(Date.now() - minutes * 60_000).toISOString();
}

function todaySunTimes(): { sunset_at: string; sunrise_at: string; is_dark: boolean } {
  const now = new Date();
  const sunset = new Date(now);
  sunset.setHours(19, 42, 0, 0);
  const sunrise = new Date(now);
  sunrise.setHours(6, 15, 0, 0);
  const is_dark = now >= sunset || now < sunrise;
  return {
    sunset_at: sunset.toISOString(),
    sunrise_at: sunrise.toISOString(),
    is_dark,
  };
}

export function createMockStoreSeed(): MockStoreSeed {
  return {
    geofences: [
      {
        geofence_id: "house",
        label: "House",
        center_lat: MOCK_HOUSE_CENTER_LAT,
        center_lon: MOCK_HOUSE_CENTER_LON,
        radius_m: MOCK_HOUSE_RADIUS_M,
        enabled: true,
        owntracks_rid: null,
      },
    ],
    participants: [
      {
        participant_id: "henrique",
        display_name: "Henrique",
        tracking_device_label: "Henrique's iPhone",
        enabled: true,
      },
      {
        participant_id: "kristen",
        display_name: "Kristen",
        tracking_device_label: "Kristen's iPhone",
        enabled: true,
      },
    ],
    participant_fixes: {
      henrique: {
        lat: MOCK_HENRIQUE_AT_HOME_LAT,
        lon: MOCK_HENRIQUE_AT_HOME_LON,
        accuracy_m: 12,
        received_at: isoMinutesAgo(1),
        source: "my-tracks",
      },
      kristen: {
        lat: MOCK_KRISTEN_OUTSIDE_LAT,
        lon: MOCK_KRISTEN_OUTSIDE_LON,
        accuracy_m: 18,
        received_at: isoMinutesAgo(5),
        source: "my-tracks",
      },
    },
    rules: [
      {
        id: "arrive-home-lights",
        label: "Welcome home — lights + garage",
        enabled: false,
        trigger: "edge_true",
        cooldown_s: 300,
        min_fix_accuracy_m: 50,
        conditions: {
          all: [
            {
              type: "participants_inside_geofence",
              geofence_id: "house",
              participant_ids: ["henrique", "kristen"],
            },
            { type: "after_sunset", offset_minutes: 0 },
          ],
        },
        device_actions: [
          {
            family_id: "kasa",
            device_id: "192.168.1.42",
            action: "turn_on",
          },
          {
            family_id: "kasa",
            device_id: "192.168.1.43",
            action: "turn_on",
          },
          {
            family_id: "tailwind",
            device_id: "main-garage",
            action: "open",
          },
        ],
      },
    ],
    rule_last_fired_at: {
      "arrive-home-lights": null,
    },
    settings_location: {
      lat: MOCK_HOUSE_CENTER_LAT,
      lon: MOCK_HOUSE_CENTER_LON,
      timezone: "America/New_York",
      home_label: "Home",
    },
    action_devices: [
      {
        family_id: "kasa",
        device_id: "192.168.1.42",
        label: "Kitchen lights",
        kind: "switch",
      },
      {
        family_id: "kasa",
        device_id: "192.168.1.43",
        label: "Porch lights",
        kind: "switch",
      },
      {
        family_id: "tailwind",
        device_id: "main-garage",
        label: "Main garage",
        kind: "door",
      },
    ],
  };
}

export function haversineM(
  lat1: number,
  lon1: number,
  lat2: number,
  lon2: number,
): number {
  const earthRadiusM = 6_371_000;
  const toRad = (deg: number): number => (deg * Math.PI) / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) * Math.sin(dLon / 2) ** 2;
  return 2 * earthRadiusM * Math.asin(Math.sqrt(a));
}

export function mockSunRow(): {
  sunset_at: string;
  sunrise_at: string;
  is_dark: boolean;
} {
  return todaySunTimes();
}
