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
  HealthOut,
  LocationHistoryRetentionIn,
  LocationHistoryRetentionOut,
  MetaOut,
  MyTracksGeofencesSyncOut,
  MyTracksLocationMonitoringIn,
  MyTracksLocationMonitoringOut,
  MyTracksPairIn,
  MyTracksPairStatusOut,
  MyTracksRelayKeySettingsOut,
  MyTracksUsersSyncOut,
  MyTracksSettingsIn,
  MyTracksSettingsOut,
  MyTracksSyncIn,
  ObservedWifiNetworkOut,
  UserHomeWifiIn,
  UserOut,
  UserStatusOut,
  RuleOut,
  RulesStatusOut,
  SettingsLocationOut,
  SmtpConfigIn,
  SmtpConfigOut,
  SmtpTestEmailIn,
  SmtpTestEmailOut,
  KasaCredentialsSetOut,
  KasaCredentialsSettingsOut,
  KasaCredentialsTestIn,
  MyTracksCredentialsTestIn,
  SettingsCredentialsTestOut,
  TailwindTokenSetOut,
  TailwindTokenSettingsOut,
  TailwindTokenTestIn,
  VizioAuthTestIn,
  VizioAuthTokenSetOut,
  VizioPairBeginOut,
  VizioPairCompleteOut,
  VizioTvsSettingsOut,
  VizioTvSettingsOut,
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

const HEALTH_FETCH_TIMEOUT_MS = 3000;
const STATE_FETCH_TIMEOUT_MS = 8000;

async function fetchWithTimeout(
  input: string,
  init: RequestInit,
  timeoutMs: number,
): Promise<Response> {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

async function call<T>(
  method: "DELETE" | "GET" | "PATCH" | "POST" | "PUT",
  path: string,
  body?: unknown,
  timeoutMs?: number,
): Promise<T> {
  const headers = authHeaders();
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const response =
    timeoutMs === undefined
      ? await fetch(path, init)
      : await fetchWithTimeout(path, init, timeoutMs);
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
  bulkOffVizio(): Promise<UIBulkActionOut> {
    return call<UIBulkActionOut>("POST", "/v1/ui/vizio/bulk-off", {});
  },
  closeAllTailwind(): Promise<UIBulkActionOut> {
    return call<UIBulkActionOut>("POST", "/v1/ui/tailwind/close-all", {});
  },
  clearKasaCredentials(): Promise<KasaCredentialsSettingsOut> {
    return call<KasaCredentialsSettingsOut>("DELETE", "/v1/settings/kasa-credentials");
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
  clearVizioAuth(deviceId: string): Promise<VizioTvSettingsOut> {
    return call<VizioTvSettingsOut>(
      "DELETE",
      `/v1/settings/vizio/auth/${encodeURIComponent(deviceId)}`,
    );
  },
  deleteRulesGeofence(geofenceId: string): Promise<void> {
    return callNoContent(
      "DELETE",
      `/v1/rules/geofences/${encodeURIComponent(geofenceId)}`,
    );
  },
  fetchHealth(): Promise<HealthOut> {
    return call<HealthOut>("GET", "/health", undefined, HEALTH_FETCH_TIMEOUT_MS);
  },
  fetchMeta(): Promise<MetaOut> {
    return call<MetaOut>("GET", "/v1/meta");
  },
  fetchMyTracksGeofencesSync(): Promise<MyTracksGeofencesSyncOut> {
    return call<MyTracksGeofencesSyncOut>("GET", "/v1/rules/geofences/sync-status");
  },
  fetchMyTracksLocationMonitoring(): Promise<MyTracksLocationMonitoringOut> {
    return call<MyTracksLocationMonitoringOut>("GET", "/v1/settings/my-tracks/location-monitoring");
  },
  fetchMyTracksPairStatus(): Promise<MyTracksPairStatusOut | null> {
    return callNullableJson<MyTracksPairStatusOut>("GET", "/v1/settings/my-tracks/pair-status");
  },
  fetchMyTracksRelayKeySettings(): Promise<MyTracksRelayKeySettingsOut> {
    return call<MyTracksRelayKeySettingsOut>("GET", "/v1/settings/my-tracks/relay-key");
  },
  fetchMyTracksUsersSync(): Promise<MyTracksUsersSyncOut> {
    return call<MyTracksUsersSyncOut>("GET", "/v1/rules/users/sync-status");
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
  patchMyTracksLocationMonitoring(
    body: MyTracksLocationMonitoringIn,
  ): Promise<MyTracksLocationMonitoringOut> {
    return call<MyTracksLocationMonitoringOut>(
      "PATCH",
      "/v1/settings/my-tracks/location-monitoring",
      body,
    );
  },
  postMyTracksPair(body: MyTracksPairIn): Promise<MyTracksPairStatusOut> {
    return call<MyTracksPairStatusOut>("POST", "/v1/settings/my-tracks/pair", body);
  },
  fetchRule(ruleId: string): Promise<RuleOut> {
    return call<RuleOut>("GET", `/v1/rules/${encodeURIComponent(ruleId)}`);
  },
  fetchRules(): Promise<RuleOut[]> {
    return call<RuleOut[]>("GET", "/v1/rules");
  },
  fetchRulesGeofences(): Promise<GeofenceOut[]> {
    return call<GeofenceOut[]>("GET", "/v1/rules/geofences");
  },
  fetchRulesUsers(): Promise<UserOut[]> {
    return call<UserOut[]>("GET", "/v1/rules/users");
  },
  fetchUserObservedWifi(userId: string): Promise<ObservedWifiNetworkOut[]> {
    return call<ObservedWifiNetworkOut[]>(
      "GET",
      `/v1/rules/users/${encodeURIComponent(userId)}/observed-wifi`,
    );
  },
  fetchRulesUserStatus(): Promise<UserStatusOut[]> {
    return call<UserStatusOut[]>("GET", "/v1/rules/users/status");
  },
  fetchRulesSettingsLocation(): Promise<SettingsLocationOut> {
    return call<SettingsLocationOut>("GET", "/v1/rules/settings/location");
  },
  fetchRulesStatus(): Promise<RulesStatusOut> {
    return call<RulesStatusOut>("GET", "/v1/rules/status");
  },
  fetchSmtpConfig(): Promise<SmtpConfigOut | null> {
    return callNullableJson<SmtpConfigOut>("GET", "/v1/settings/smtp");
  },
  fetchState(): Promise<UIStateOut> {
    return call<UIStateOut>(
      "GET",
      "/v1/ui/state",
      undefined,
      STATE_FETCH_TIMEOUT_MS,
    );
  },
  fetchKasaCredentialsSettings(): Promise<KasaCredentialsSettingsOut> {
    return call<KasaCredentialsSettingsOut>("GET", "/v1/settings/kasa-credentials");
  },
  fetchTailwindTokenSettings(): Promise<TailwindTokenSettingsOut> {
    return call<TailwindTokenSettingsOut>("GET", "/v1/settings/tailwind-token");
  },
  fetchVizioTvsSettings(): Promise<VizioTvsSettingsOut> {
    return call<VizioTvsSettingsOut>("GET", "/v1/settings/vizio/tvs");
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
  putUserHomeWifi(
    userId: string,
    body: UserHomeWifiIn,
  ): Promise<UserOut> {
    return call<UserOut>(
      "PUT",
      `/v1/rules/users/${encodeURIComponent(userId)}/home-wifi`,
      body,
    );
  },
  putKasaCredentials(
    username: string,
    password: string,
  ): Promise<KasaCredentialsSetOut> {
    return call<KasaCredentialsSetOut>("PUT", "/v1/settings/kasa-credentials", {
      username,
      password,
    });
  },
  putTailwindToken(token: string): Promise<TailwindTokenSetOut> {
    return call<TailwindTokenSetOut>("PUT", "/v1/settings/tailwind-token", { token });
  },
  putVizioAuthToken(deviceId: string, token: string): Promise<VizioAuthTokenSetOut> {
    return call<VizioAuthTokenSetOut>(
      "PUT",
      `/v1/settings/vizio/tvs/${encodeURIComponent(deviceId)}/auth`,
      { token },
    );
  },
  beginVizioPairing(host: string): Promise<VizioPairBeginOut> {
    return call<VizioPairBeginOut>("POST", "/v1/settings/vizio/pair/begin", { host });
  },
  completeVizioPairing(body: {
    device_id: string;
    pin: string;
    challenge_type: number;
    pairing_req_token: number;
  }): Promise<VizioPairCompleteOut> {
    return call<VizioPairCompleteOut>("POST", "/v1/settings/vizio/pair/complete", body);
  },
  cancelVizioPairing(body: {
    device_id: string;
    challenge_type: number;
    pairing_req_token: number;
  }): Promise<{ ok: boolean }> {
    return call<{ ok: boolean }>("POST", "/v1/settings/vizio/pair/cancel", body);
  },
  sendSmtpTestEmail(input: SmtpTestEmailIn): Promise<SmtpTestEmailOut> {
    return call<SmtpTestEmailOut>("POST", "/v1/settings/smtp/test", input);
  },
  testKasaCredentials(
    input: KasaCredentialsTestIn = {},
  ): Promise<SettingsCredentialsTestOut> {
    return call<SettingsCredentialsTestOut>(
      "POST",
      "/v1/settings/kasa-credentials/test",
      input,
    );
  },
  testMyTracksCredentials(
    input: MyTracksCredentialsTestIn,
  ): Promise<SettingsCredentialsTestOut> {
    return call<SettingsCredentialsTestOut>(
      "POST",
      "/v1/settings/my-tracks/test",
      input,
    );
  },
  testTailwindToken(
    input: TailwindTokenTestIn = {},
  ): Promise<SettingsCredentialsTestOut> {
    return call<SettingsCredentialsTestOut>(
      "POST",
      "/v1/settings/tailwind-token/test",
      input,
    );
  },
  testVizioAuth(
    deviceId: string,
    input: VizioAuthTestIn = {},
  ): Promise<SettingsCredentialsTestOut> {
    return call<SettingsCredentialsTestOut>(
      "POST",
      `/v1/settings/vizio/tvs/${encodeURIComponent(deviceId)}/auth/test`,
      input,
    );
  },
  syncMyTracksGeofences(credentials: MyTracksSyncIn): Promise<MyTracksGeofencesSyncOut> {
    return call<MyTracksGeofencesSyncOut>(
      "POST",
      "/v1/rules/geofences/sync",
      credentials,
    );
  },
  syncMyTracksUsers(
    credentials: MyTracksSyncIn,
  ): Promise<MyTracksUsersSyncOut> {
    return call<MyTracksUsersSyncOut>(
      "POST",
      "/v1/rules/users/sync",
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
  toggleDevice(familyId: string, deviceId: string): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/devices/${encodeURIComponent(familyId)}/${encodeURIComponent(deviceId)}/toggle`,
      {},
    );
  },
};

function isBackendTransportFailure(err: unknown): boolean {
  // Any HTTP status means the TCP/HTTP stack reached the server — that is
  // not "can't connect", even for 401/503/500.
  if (err instanceof HttpError) {
    return false;
  }
  if (err instanceof DOMException) {
    return err.name === "AbortError";
  }
  if (err instanceof TypeError) {
    return true;
  }
  return false;
}

export { HttpError, isBackendTransportFailure };
