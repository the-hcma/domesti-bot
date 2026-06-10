// Thin fetch wrappers around the FastAPI surface declared in
// `app/api/app.py`. Every helper returns the JSON-decoded payload typed
// against the matching interface in `./types.ts`.
//
// Auth: when the server is started with `DOMESTI_API_KEY=…`, every
// protected `/v1/...` route (everything except ``GET /v1/meta``) requires
// the `X-Domesti-Api-Key` header. The browser
// reads the key from a `<meta name="domesti-api-key" content="…">` tag if
// present (so a deployment can inject it server-side without us exposing
// it to JS at build time). Default LAN deployments leave the env var
// unset and the page works without the meta tag.

import type {
  GeofenceOut,
  LocationHistoryRetentionIn,
  LocationHistoryRetentionOut,
  MetaOut,
  MyTracksGeofencesSyncOut,
  MyTracksPairIn,
  MyTracksPairStatusOut,
  MyTracksRelayKeySettingsOut,
  MyTracksParticipantsSyncOut,
  MyTracksSettingsIn,
  MyTracksSettingsOut,
  MyTracksSyncIn,
  ParticipantOut,
  ParticipantStatusOut,
  SmtpConfigIn,
  SmtpConfigOut,
  SmtpTestEmailIn,
  SmtpTestEmailOut,
  TailwindTokenSetOut,
  TailwindTokenSettingsOut,
  UIBulkActionOut,
  UIDeviceActionOut,
  UIGlobalBulkActionOut,
  UIPreferenceOut,
  UIStateOut,
} from "./types.js";

class HttpError extends Error {
  readonly status: number;
  readonly bodyText: string;
  // Parsed FastAPI ``{"detail": "..."}`` payload, or the raw body
  // when the response isn't the JSON-detail shape. Memoised so
  // callers that read it from multiple places (toast renderer +
  // logging) don't re-parse on every access. Always a string, never
  // null — falls back to ``bodyText`` per :func:`parseDetail`.
  readonly detail: string;

  constructor(status: number, bodyText: string) {
    super(`HTTP ${status}: ${bodyText.slice(0, 160)}`);
    this.name = "HttpError";
    this.status = status;
    this.bodyText = bodyText;
    this.detail = parseDetail(bodyText);
  }

  // ``/v1/ui/state`` returns 503 with two flavors of detail string
  // (see ``app.api.app._device_state``):
  //   * "Device discovery still in progress; ..."  → bootstrap, retry
  //   * "Device discovery failed: ..."             → permanent, surface
  // The frontend bootstrap loop keeps spinning only while the first
  // flavor is in play; everything else (other 503 detail, network
  // error, auth failure, 500, ...) bails out to the error banner.
  isDiscoveryInProgress(): boolean {
    if (this.status !== 503) return false;
    return this.detail.toLowerCase().includes("still in progress");
  }
}

function parseDetail(bodyText: string): string {
  // FastAPI's default ``HTTPException`` serializer returns
  // ``{"detail": "..."}``; we tolerate a non-JSON body so a future
  // middleware that wraps the response can't break the loop.
  try {
    const parsed: unknown = JSON.parse(bodyText);
    if (parsed && typeof parsed === "object" && "detail" in parsed) {
      const d = (parsed as { detail: unknown }).detail;
      if (typeof d === "string") return d;
    }
  } catch {
    // bodyText wasn't JSON — fall through and search the raw text.
  }
  return bodyText;
}

function apiKeyFromMeta(): string | null {
  const meta = document.querySelector<HTMLMetaElement>(
    'meta[name="domesti-api-key"]',
  );
  const v = meta?.content?.trim();
  return v ? v : null;
}

/** Headers for protected ``/v1/...`` routes (rules wire-up, manual fetch). */
export function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  const apiKey = apiKeyFromMeta();
  if (apiKey) {
    headers["X-Domesti-Api-Key"] = apiKey;
  }
  return headers;
}

async function call<T>(
  method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT",
  path: string,
  body?: unknown,
): Promise<T> {
  const headers = authHeaders();
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response = await fetch(path, init);
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new HttpError(response.status, text);
  }
  return (await response.json()) as T;
}

async function callNoContent(
  method: "DELETE",
  path: string,
): Promise<void> {
  const response = await fetch(path, { method, headers: authHeaders() });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new HttpError(response.status, text);
  }
}

async function callNullableJson<T>(
  method: "GET",
  path: string,
): Promise<T | null> {
  const response = await fetch(path, { method, headers: authHeaders() });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new HttpError(response.status, text);
  }
  const text = await response.text();
  if (text === "" || text === "null") {
    return null;
  }
  return JSON.parse(text) as T;
}

export const api = {
  bulkOffGlobal(): Promise<UIGlobalBulkActionOut> {
    return call<UIGlobalBulkActionOut>("POST", "/v1/ui/global/bulk-off", {});
  },
  bulkOffKasa(): Promise<UIBulkActionOut> {
    return call<UIBulkActionOut>("POST", "/v1/ui/kasa/bulk-off", {});
  },
  closeAllTailwind(): Promise<UIBulkActionOut> {
    return call<UIBulkActionOut>("POST", "/v1/ui/tailwind/close-all", {});
  },
  closeTailwindDoor(deviceId: string): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/tailwind/doors/${encodeURIComponent(deviceId)}/close`,
      {},
    );
  },
  clearSmtpConfig(): Promise<void> {
    return callNoContent("DELETE", "/v1/settings/smtp");
  },
  clearMyTracksPairing(): Promise<MyTracksPairStatusOut | null> {
    return call<MyTracksPairStatusOut | null>("DELETE", "/v1/settings/my-tracks/pair");
  },
  clearMyTracksSettings(): Promise<void> {
    return callNoContent("DELETE", "/v1/settings/my-tracks");
  },
  clearTailwindToken(): Promise<TailwindTokenSettingsOut> {
    return call<TailwindTokenSettingsOut>("DELETE", "/v1/settings/tailwind-token");
  },
  deleteRulesGeofence(geofenceId: string): Promise<void> {
    return callNoContent(
      "DELETE",
      `/v1/rules/geofences/${encodeURIComponent(geofenceId)}`,
    );
  },
  fetchMeta(): Promise<MetaOut> {
    return call<MetaOut>("GET", "/v1/meta");
  },
  fetchMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return call<MyTracksGeofencesSyncOut>("GET", "/v1/rules/geofences/sync-status");
  },
  fetchMyTracksPairStatus(): Promise<MyTracksPairStatusOut | null> {
    return callNullableJson<MyTracksPairStatusOut>("GET", "/v1/settings/my-tracks/pair-status");
  },
  fetchMyTracksRelayKeySettings(): Promise<MyTracksRelayKeySettingsOut> {
    return call<MyTracksRelayKeySettingsOut>("GET", "/v1/settings/my-tracks/relay-key");
  },
  fetchMyTracksParticipantsSync(): Promise<MyTracksParticipantsSyncOut> {
    return call<MyTracksParticipantsSyncOut>("GET", "/v1/rules/participants/sync-status");
  },
  fetchMyTracksSettings(): Promise<MyTracksSettingsOut | null> {
    return callNullableJson<MyTracksSettingsOut>("GET", "/v1/settings/my-tracks");
  },
  patchMyTracksLocationHistoryRetention(
    body: LocationHistoryRetentionIn,
  ): Promise<LocationHistoryRetentionOut> {
    return call<LocationHistoryRetentionOut>(
      "PATCH",
      "/v1/settings/my-tracks/location-history-retention",
      body,
    );
  },
  postMyTracksPair(body: MyTracksPairIn): Promise<MyTracksPairStatusOut> {
    return call<MyTracksPairStatusOut>("POST", "/v1/settings/my-tracks/pair", body);
  },
  fetchRulesGeofences(): Promise<GeofenceOut[]> {
    return call<GeofenceOut[]>("GET", "/v1/rules/geofences");
  },
  fetchRulesParticipants(): Promise<ParticipantOut[]> {
    return call<ParticipantOut[]>("GET", "/v1/rules/participants");
  },
  fetchRulesParticipantStatus(): Promise<ParticipantStatusOut[]> {
    return call<ParticipantStatusOut[]>("GET", "/v1/rules/participants/status");
  },
  fetchSmtpConfig(): Promise<SmtpConfigOut | null> {
    return callNullableJson<SmtpConfigOut>("GET", "/v1/settings/smtp");
  },
  fetchState(): Promise<UIStateOut> {
    return call<UIStateOut>("GET", "/v1/ui/state");
  },
  fetchTailwindTokenSettings(): Promise<TailwindTokenSettingsOut> {
    return call<TailwindTokenSettingsOut>("GET", "/v1/settings/tailwind-token");
  },
  openTailwindDoor(deviceId: string): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/tailwind/doors/${encodeURIComponent(deviceId)}/open`,
      {},
    );
  },
  pauseAllSonos(): Promise<UIBulkActionOut> {
    return call<UIBulkActionOut>("POST", "/v1/ui/sonos/pause-all", {});
  },
  putSmtpConfig(config: SmtpConfigIn): Promise<SmtpConfigOut> {
    return call<SmtpConfigOut>("PUT", "/v1/settings/smtp", config);
  },
  putMyTracksSettings(config: MyTracksSettingsIn): Promise<MyTracksSettingsOut> {
    return call<MyTracksSettingsOut>("PUT", "/v1/settings/my-tracks", config);
  },
  putRulesGeofence(geofence: GeofenceOut): Promise<GeofenceOut> {
    return call<GeofenceOut>(
      "PUT",
      `/v1/rules/geofences/${encodeURIComponent(geofence.geofence_id)}`,
      geofence,
    );
  },
  putTailwindToken(token: string): Promise<TailwindTokenSetOut> {
    return call<TailwindTokenSetOut>("PUT", "/v1/settings/tailwind-token", { token });
  },
  sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut> {
    return call<SmtpTestEmailOut>("POST", "/v1/settings/smtp/test", input);
  },
  syncMyTracksGeofences(credentials: MyTracksSyncIn): Promise<MyTracksGeofencesSyncOut> {
    return call<MyTracksGeofencesSyncOut>(
      "POST",
      "/v1/rules/geofences/sync",
      credentials,
    );
  },
  syncMyTracksParticipants(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksParticipantsSyncOut> {
    return call<MyTracksParticipantsSyncOut>(
      "POST",
      "/v1/rules/participants/sync",
      credentials,
    );
  },
  setExclude(
    familyId: string,
    deviceId: string,
    excludeFromGlobal: boolean,
  ): Promise<UIPreferenceOut> {
    return call<UIPreferenceOut>(
      "PUT",
      `/v1/ui/preferences/${encodeURIComponent(familyId)}/${encodeURIComponent(deviceId)}`,
      { exclude_from_global: excludeFromGlobal },
    );
  },
  toggleKasa(deviceId: string, on: boolean): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/kasa/devices/${encodeURIComponent(deviceId)}/toggle`,
      { on },
    );
  },
  toggleSonos(
    deviceId: string,
    playing: boolean,
    favoriteIndex = 0,
  ): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/sonos/zones/${encodeURIComponent(deviceId)}/toggle`,
      playing
        ? { playing: true, favorite_index: favoriteIndex }
        : { playing: false },
    );
  },
};

export { HttpError };
