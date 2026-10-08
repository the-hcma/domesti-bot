// Scoped API keys in the browser (design: docs/API_KEY_SCOPES.md).
//
// The page carries only the control key (the `<meta name="domesti-api-key">` tag). Routes that need the
// admin scope (settings, `execute-line`, the My Tracks sync routes) use a key the operator types into a
// prompt; it is held in this module's memory only (never the DOM, storage or a cookie) and a reload asks
// again. When only an admin key is configured the page has no meta key and the typed key is used for every
// protected route. This module has no DOM or network dependencies so it can be tested with `node --test`.

const ADMIN_EXACT_PATHS: ReadonlySet<string> = new Set([
  "/v1/execute-line",
  "/v1/rules/geofences/sync",
  "/v1/rules/users/sync",
]);

let typedKey: string | null = null;

export interface ProtectedFetchDeps {
  fetchImpl: (path: string, init: RequestInit) => Promise<Response>;
  metaKey: () => string | null;
  promptForKey: (message: string) => Promise<string | null>;
  /** Clock for the decline cooldown (tests inject one). */
  now?: () => number;
}

/** True for routes that need the admin scope. */
export function isAdminRoute(path: string): boolean {
  const bare = path.split("?")[0] ?? path;
  return bare === "/v1/settings" || bare.startsWith("/v1/settings/") || ADMIN_EXACT_PATHS.has(bare);
}

export function getTypedKey(): string | null {
  return typedKey;
}

export function setTypedKey(key: string | null): void {
  const trimmed = key?.trim();
  typedKey = trimmed ? trimmed : null;
}

export function clearTypedKey(): void {
  typedKey = null;
}

/** The key to send for a path: the typed key first on admin routes, the meta key first elsewhere. */
export function keyForRequest(path: string, metaKey: string | null): string | null {
  return isAdminRoute(path) ? (typedKey ?? metaKey) : (metaKey ?? typedKey);
}

async function requiredScope(response: Response): Promise<string | null> {
  try {
    const parsed: unknown = await response.clone().json();
    if (parsed && typeof parsed === "object" && "required_scope" in parsed) {
      const scope = (parsed as { required_scope: unknown }).required_scope;
      return typeof scope === "string" ? scope : null;
    }
  } catch {
    // Not JSON: no scope information.
  }
  return null;
}

function withKey(init: RequestInit, key: string | null): RequestInit {
  const headers = new Headers(init.headers);
  headers.delete("X-Domesti-Api-Key");
  if (key) {
    headers.set("X-Domesti-Api-Key", key);
  }
  return { ...init, headers };
}

const MAX_PROMPTS_PER_REQUEST = 2;
/** After the operator cancels the prompt, other requests fail quietly for this long instead of re-prompting. */
export const DECLINE_COOLDOWN_MS = 30_000;

let declinedAt: number | null = null;

export function resetPromptState(): void {
  declinedAt = null;
}

/**
 * `fetch` for a protected route. On a `403` naming the `admin` scope, or a `401` that a typed key could
 * fix, it asks for the key and retries (at most twice). Declining the prompt returns the original response
 * and silences further prompts for `DECLINE_COOLDOWN_MS`. A response to a request sent with a key that has
 * since been replaced (a concurrent request typed it) is retried with the current key, not prompted for.
 */
export async function fetchProtected(
  path: string,
  init: RequestInit,
  deps: ProtectedFetchDeps,
): Promise<Response> {
  const now = deps.now ?? Date.now;
  let sentKey = keyForRequest(path, deps.metaKey());
  let response = await deps.fetchImpl(path, withKey(init, sentKey));
  for (let attempts = 0; attempts < MAX_PROMPTS_PER_REQUEST; attempts += 1) {
    const message = await promptMessageFor(path, response, deps.metaKey());
    if (message === null) {
      return response;
    }
    const currentKey = keyForRequest(path, deps.metaKey());
    if (currentKey !== sentKey && currentKey !== null) {
      // Another request already typed a key; this response says nothing about it. Retry with it.
      sentKey = currentKey;
      response = await deps.fetchImpl(path, withKey(init, sentKey));
      continue;
    }
    if (response.status === 401 && sentKey !== null && sentKey === typedKey) {
      // The server rejected the key we typed: drop it even when the prompt is suppressed by the cooldown.
      clearTypedKey();
    }
    if (declinedAt !== null && now() - declinedAt < DECLINE_COOLDOWN_MS) {
      return response;
    }
    const entered = await deps.promptForKey(message);
    if (!entered || !entered.trim()) {
      declinedAt = now();
      return response;
    }
    declinedAt = null;
    setTypedKey(entered);
    sentKey = keyForRequest(path, deps.metaKey());
    response = await deps.fetchImpl(path, withKey(init, sentKey));
  }
  return response;
}

async function promptMessageFor(
  path: string,
  response: Response,
  metaKey: string | null,
): Promise<string | null> {
  if (response.status === 403 && (await requiredScope(response)) === "admin") {
    return "This needs an API key with admin access. Enter it to continue (it is kept in memory for this page only).";
  }
  if (response.status !== 401) {
    return null;
  }
  if (isAdminRoute(path)) {
    return typedKey
      ? "That API key was not accepted. Enter it again."
      : "This needs an API key with admin access (the admin key, or the control key if no admin key is configured). It is kept in memory for this page only.";
  }
  // A non-admin route: only a typed key (admin-only deployments have no meta key) can be fixed here.
  if (!metaKey) {
    return typedKey
      ? "That API key was not accepted. Enter it again."
      : "This server needs an API key. Enter it to continue (it is kept in memory for this page only).";
  }
  return null;
}
