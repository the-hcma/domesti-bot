// Seed fixtures for the motivating Henrique + Kristen arrival rule scenario.
// Coordinates match ``tests/python/test_rule_engine.py``.

import {
  UIDeviceKind,
  type GeofenceOut,
  type RuleActionDeviceOut,
  type RuleOut,
  type SettingsLocationOut,
  type TimeConditionTemplateOut,
  type UserLocationOut,
  type UserOut,
  type VacationModeSettingsStatusOut,
} from "./types.js";

export interface MockSmtpConfig {
  host: string;
  port: number;
  username: string;
  password: string;
  mail_domain: string;
  from_address: string;
}

export interface MockUsersSync {
  last_synced_at: string | null;
}

export interface MockGeofencesSync {
  last_synced_at: string | null;
}

export interface MockMyTracksSettings {
  domain: string;
  username: string;
}

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
  users: UserOut[];
  user_locations: Record<string, UserLocationOut>;
  rules: RuleOut[];
  rule_last_fired_at: Record<string, string | null>;
  settings_location: SettingsLocationOut;
  action_devices: RuleActionDeviceOut[];
  time_condition_templates: TimeConditionTemplateOut[];
  smtp_config: MockSmtpConfig | null;
  smtp_last_test_recipient: string | null;
  vacation_mode: VacationModeSettingsStatusOut;
  my_tracks_user_catalog: UserOut[];
  my_tracks_geofence_catalog: GeofenceOut[];
  my_tracks_settings: MockMyTracksSettings | null;
  users_sync: MockUsersSync;
  geofences_sync: MockGeofencesSync;
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

const MOCK_MY_TRACKS_USERS: UserOut[] = [
  {
    user_id: "henrique",
    first_name: "Henrique",
    last_name: "",
    display_name: "Henrique",
    tracking_device_label: "Henrique's iPhone",
    enabled: true,
    home_wifi_bssid: null,
    home_wifi_ssid: null,
  },
  {
    user_id: "kristen",
    first_name: "Kristen",
    last_name: "",
    display_name: "Kristen",
    tracking_device_label: "Kristen's iPhone",
    enabled: true,
    home_wifi_bssid: null,
    home_wifi_ssid: null,
  },
];

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
    my_tracks_user_catalog: structuredClone(MOCK_MY_TRACKS_USERS),
    users: structuredClone(MOCK_MY_TRACKS_USERS),
    my_tracks_geofence_catalog: [
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
    my_tracks_settings: null,
    users_sync: {
      last_synced_at: isoMinutesAgo(30),
    },
    geofences_sync: {
      last_synced_at: null,
    },
    user_locations: {
      henrique: {
        lat: MOCK_HENRIQUE_AT_HOME_LAT,
        lon: MOCK_HENRIQUE_AT_HOME_LON,
        accuracy_m: 12,
        fix_at: isoMinutesAgo(1),
        reported_at: isoMinutesAgo(1),
        source: "my-tracks",
      },
      kristen: {
        lat: MOCK_KRISTEN_OUTSIDE_LAT,
        lon: MOCK_KRISTEN_OUTSIDE_LON,
        accuracy_m: 18,
        fix_at: isoMinutesAgo(5),
        reported_at: isoMinutesAgo(5),
        source: "my-tracks",
      },
    },
    rules: [
      {
        id: "arrive-home-lights",
        label: "Welcome home — lights + garage",
        enabled: false,
        triggers: ["edge_true"],
        schedule_cron: null,
        cooldown_s: 300,
        min_location_accuracy_m: 50,
        notify_on_fire: false,
        notification_emails: [],
        conditions: {
          all: [
            {
              type: "users_inside_geofence",
              geofence_id: "house",
              user_ids: ["henrique", "kristen"],
            },
            { type: "after_sunset", offset_minutes: 0, window_end: "midnight" },
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
      home_configured: true,
      lat: MOCK_HOUSE_CENTER_LAT,
      lon: MOCK_HOUSE_CENTER_LON,
      timezone: "America/New_York",
      home_label: "Home",
    },
    vacation_mode: {
      armed: false,
      enabled: false,
      hysteresis_s: 1800,
      min_distance_m: 80_000,
      min_location_accuracy_m: 50,
      notification_emails: ["operator@example.com"],
      notify_on_transition: true,
      user_ids: ["henrique", "kristen"],
    },
    smtp_config: null,
    smtp_last_test_recipient: null,
    time_condition_templates: [
      {
        template_id: "weeknight-quiet",
        label: "Weeknight quiet hours",
        start_hhmm: "22:00",
        end_hhmm: "06:00",
      },
    ],
    action_devices: [
      {
        family_id: "kasa",
        device_id: "192.168.1.42",
        label: "Kitchen lights",
        kind: UIDeviceKind.Switch,
      },
      {
        family_id: "kasa",
        device_id: "192.168.1.43",
        label: "Porch lights",
        kind: UIDeviceKind.Switch,
      },
      {
        family_id: "tailwind",
        device_id: "main-garage",
        label: "Main garage",
        kind: UIDeviceKind.Door,
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
