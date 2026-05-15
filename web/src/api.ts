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
  MetaOut,
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

async function call<T>(
  method: "DELETE" | "GET" | "POST" | "PUT",
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = {
    Accept: "application/json",
  };
  const apiKey = apiKeyFromMeta();
  if (apiKey) {
    headers["X-Domesti-Api-Key"] = apiKey;
  }
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
  fetchMeta(): Promise<MetaOut> {
    return call<MetaOut>("GET", "/v1/meta");
  },
  clearTailwindToken(): Promise<TailwindTokenSettingsOut> {
    return call<TailwindTokenSettingsOut>("DELETE", "/v1/settings/tailwind-token");
  },
  fetchState(): Promise<UIStateOut> {
    return call<UIStateOut>("GET", "/v1/ui/state");
  },
  fetchTailwindTokenSettings(): Promise<TailwindTokenSettingsOut> {
    return call<TailwindTokenSettingsOut>("GET", "/v1/settings/tailwind-token");
  },
  putTailwindToken(token: string): Promise<TailwindTokenSetOut> {
    return call<TailwindTokenSetOut>("PUT", "/v1/settings/tailwind-token", { token });
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
  toggleSonos(deviceId: string, playing: boolean): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/sonos/zones/${encodeURIComponent(deviceId)}/toggle`,
      { playing },
    );
  },
};

export { HttpError };
