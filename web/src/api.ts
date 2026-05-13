// Thin fetch wrappers around the FastAPI surface declared in
// `app/api/app.py`. Every helper returns the JSON-decoded payload typed
// against the matching interface in `./types.ts`.
//
// Auth: when the server is started with `DOMESTI_API_KEY=…`, every
// `/v1/...` route requires the `X-Domesti-Api-Key` header. The browser
// reads the key from a `<meta name="domesti-api-key" content="…">` tag if
// present (so a deployment can inject it server-side without us exposing
// it to JS at build time). Default LAN deployments leave the env var
// unset and the page works without the meta tag.

import type {
  UIBulkActionOut,
  UIDeviceActionOut,
  UIGlobalBulkActionOut,
  UIPreferenceOut,
  UIStateOut,
} from "./types.js";

class HttpError extends Error {
  readonly status: number;
  readonly bodyText: string;

  constructor(status: number, bodyText: string) {
    super(`HTTP ${status}: ${bodyText.slice(0, 160)}`);
    this.name = "HttpError";
    this.status = status;
    this.bodyText = bodyText;
  }
}

function apiKeyFromMeta(): string | null {
  const meta = document.querySelector<HTMLMetaElement>(
    'meta[name="domesti-api-key"]',
  );
  const v = meta?.content?.trim();
  return v ? v : null;
}

async function call<T>(
  method: "GET" | "POST" | "PUT",
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
  fetchState(): Promise<UIStateOut> {
    return call<UIStateOut>("GET", "/v1/ui/state");
  },
  openTailwindDoor(deviceId: string): Promise<UIDeviceActionOut> {
    return call<UIDeviceActionOut>(
      "POST",
      `/v1/ui/tailwind/doors/${encodeURIComponent(deviceId)}/open`,
      {},
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
};

export { HttpError };
