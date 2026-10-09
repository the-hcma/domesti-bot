# Scoped API keys

Status: implemented (#793). The server-side scopes landed in #804, the web client prompt in #805 and the CLI flag and unit-file notes in #807. This document is the design record; the sections below describe the behavior that shipped, and what was deliberately left out is under "Deferred".

## Problem

One `DOMESTI_API_KEY` guards the whole protected API, so the key the browser holds can also write every stored credential, pair with My Tracks, and run any REPL line through `POST /v1/execute-line`. The web client reads that key from a `<meta name="domesti-api-key">` tag. No code in this repository emits the tag, so whatever serves the page (a reverse proxy, a deployment script) injects it, and anything that can run script in the page or read the served HTML has the settings write surface. The Content-Security-Policy (`script-src 'self'`) narrows that but does not remove it, and a key leaked through a log, a screenshot or a script on the LAN is a full compromise.

When no key is configured the API is open by design (trusted LAN). That mode is unchanged by this design, apart from the startup warning described below.

## Goals and non-goals

- The key a page carries must not be able to read or write credentials, pair, or run arbitrary REPL lines.
- A deployment that sets only `DOMESTI_API_KEY` keeps working with no change, including the web Settings pages.
- Keys stay in the environment, never in the database, so rotation is a restart.
- Non-goals: user accounts, per-user audit identities, cookie sessions and OAuth (see "Deferred").

## Scopes

Three scopes, each implied by the next: `read` is implied by `control`, which is implied by `admin`.

## Route inventory

This is the complete inventory as of this design (78 routes in the routers plus the paths below). The implementation PR generates the same table from the app and fails if it differs, so it cannot drift.

Public, no key, unchanged: `GET /`, `GET /sw.js`, `GET /favicon.ico`, `GET /health`, `GET /v1/meta`, everything under `/static`, and the generated `/docs`, `/redoc` and `/openapi.json`. The schema pages expose route names, not data; hiding them when a key is configured is a possible follow-up, not part of this change. `OPTIONS` (CORS preflight) never needs a key. `HEAD` is not served at all: FastAPI routes do not add `HEAD` to a `GET` route, so every route answers `HEAD` with `405` before any dependency runs (public and protected alike). A test pins this; if a route ever starts serving `HEAD`, it must inherit that route's `GET` scope.

Authenticated by the relay key, not by this scheme, and never touched by the scope resolver: `POST /v1/webhooks/location_update` and `POST /v1/webhooks/location_update/test` (`verify_mytracks_relay_api_key` reads the same header name against the stored relay secret).

| Scope | Routes |
| --- | --- |
| `read` | `GET /v1/ui/state`, `GET /v1/completion-aliases`, every `GET` under `/v1/rules` (rules, status, validation, users, geofences, settings/location, settings/vacation-mode, sync-status, observed-wifi) and under `/v1/sensor-collection` |
| `control` | every `POST`, `PUT` and `DELETE` under `/v1/ui/**` (toggles, bulk-off, pause-all, doors, preferences); `PUT /v1/location_update/{user_id}`; the writes under `/v1/rules` (geofence `PUT`/`DELETE`, `settings/location`, `settings/vacation-mode` and its test, users `home-wifi` and `household`); the writes under `/v1/sensor-collection` (retention, prune-preview, sensor `PUT`) |
| `admin` | everything under `/v1/settings/**` for every method, including `GET` (discovery, EP1 PSK, Kasa credentials, My Tracks settings, pairing, SMTP, Tailwind token, Vizio pairing and tokens, key status); `POST /v1/execute-line`; `POST /v1/rules/geofences/sync` and `POST /v1/rules/users/sync`, which carry My Tracks administrator credentials in the body |

Reasons for the less obvious rows. `POST /v1/execute-line` runs any REPL line, including ones that change settings, so it cannot be less privileged than what it can do. The two sync routes live under `/v1/rules` (the `mytracks_rules_router`) but accept credentials, so they are `admin` by route, not by prefix. `GET /v1/settings/**` is `admin` because, although no settings response returns a secret any more, they reveal topology, key status and pairing state. Reads under `/v1/rules` return user data (names, Wi-Fi, geofences); they stay `read` on purpose, and a deployment that wants them narrower raises the read bar by not issuing a read key.

## Keys

| Variable | Grants | When unset |
| --- | --- | --- |
| `DOMESTI_ADMIN_API_KEY` | `admin` (and so `control` and `read`) | `admin` is granted by the `DOMESTI_API_KEY` value, so nothing changes |
| `DOMESTI_API_KEY` | `control` (and `read`) | no control key; see the rules below |
| `DOMESTI_READ_API_KEY` | `read` only | no read-only key exists |

Rules:

- Values are stripped; blank or whitespace-only values count as unset (as `_expected_api_key` does today).
- Keyed mode: if any of the three variables is set, every non-public route needs a key whose scope is high enough. Open mode (none set) is exactly today's behavior.
- `DOMESTI_ADMIN_API_KEY` alone is valid and fail-closed: it is the only key, so it is needed for every non-public route (the browser then needs it everywhere, which is the operator's choice).
- `DOMESTI_READ_API_KEY` without a control or admin key is a startup error, because control and admin routes would otherwise have no key that can reach them.
- The same value in two variables is allowed with a startup warning; the higher scope applies, which is what the fallback already does.
- Resolution compares the presented key against every configured key (constant time per pair, no early return) and then takes the highest matching scope, so which scope matched is not observable through timing. Key length is not hidden, as with `api_keys_match` today.
- Startup logs depend on the mode. With `DOMESTI_API_KEY` set and `DOMESTI_ADMIN_API_KEY` unset it logs one line ("admin routes share the control key; set DOMESTI_ADMIN_API_KEY to separate them"). With no key at all (open mode) it logs a different line saying the API is open and that every route, including settings, is reachable without a key, and the existing wildcard-bind warning in `config/serve.py` fires when listening on all interfaces. That warning treats any configured key as keyed.

## Status codes

- A request with no key, or a key matching no configured key, is `401` on every protected route, including admin routes. A mistyped admin key is `401`, not `403`.
- A valid key whose scope is too low is `403` with `{"detail": "...", "required_scope": "admin"}`. The body names only the scope needed, nothing about other keys.
- Existing tests that assert `401` for a missing or wrong key keep passing.

## How the browser gets keys

- When `DOMESTI_API_KEY` is configured, the page keeps receiving only the `control` key through the existing meta tag, so no deployment has to change how it serves the page. When only `DOMESTI_ADMIN_API_KEY` is configured there is no control key to embed: the client prompts before its first protected request and uses the entered key for every protected route (read, control and admin), and a `401` from any protected route in that mode clears it. Open mode has no prompt.
- The Settings menu and the Rules sync actions need the `admin` key. The client holds it in a module variable (memory only), sends it only on `/v1/settings/**`, `/v1/execute-line` and the two sync routes (every protected route in admin-only mode), and drops it on a `401` from those routes. A page reload asks again; that is deliberate. Any script running in the page can read memory, `sessionStorage` or a cookie alike, so the protection is for the idle page, leaked HTML, logs and screenshots, not for a live XSS, and the doc and UI do not claim more.
- The prompt appears when a Settings or Rules sync view opens, not only after a failed write, because those views read credential status on render. The client distinguishes "the meta key is bad" (`401` on a non-admin route) from "the admin key is bad" (`401` on an admin route) so it clears only the right one.
- Two existing client paths must change, or the prompt never fires. `settingsApiAvailable()` and `rulesApiAvailable()` in `web/src/rules-data-source.ts` probe the API and accept only `ok` or `401` as "available"; they must treat a `403` carrying `required_scope` as available. The request helpers in `web/src/api.ts` (`call`, `callNoContent`, `callNullableJson`) gain per-request key selection and a typed error for `403`.
- If no admin key is configured, the control key already satisfies `admin` and the prompt never appears.
- The admin key reuses the `X-Domesti-Api-Key` header, so the CORS allow-list from #796 needs no change.

## Attackers

| Attacker | Today | With scopes |
| --- | --- | --- |
| Script in the page that is not actively attacking, or someone reading the served HTML | Full API including all credential writes and REPL lines | Device control only; no settings, pairing, sync or REPL |
| Leaked `control` key (log, screenshot) | Full API | Device control only |
| Live XSS in the page while an admin has typed the key | Full API | Full API (memory is readable by page script); the exposure window is the typed session, not every page load |
| Leaked `admin` key | Full API | Full API, rotated alone |
| LAN attacker with no key, a key configured | `401` everywhere | Same |
| LAN attacker, open mode | Everything | Everything (unchanged; configure a key). Startup warns when listening on all interfaces |

## Implementation

1. **Scope dependency.** `app/api/api_scopes.py`: `require_scope("read" | "control" | "admin")` and the router-level `require_scope_by_method`, built on one resolver that reads the key once, compares it against every configured key and takes the highest scope; `DOMESTI_API_KEY` also grants admin while `DOMESTI_ADMIN_API_KEY` is unset. Every router and route in `app/api/app.py` declares its scope, and the two My Tracks sync routes override their router's scope.
2. **Coverage test.** `tests/python/test_api_key_scopes.py` sends every OpenAPI route every kind of key against an independent copy of the table above; an unclassified route fails the suite. It also pins that `HEAD` is not served, that the public paths stay public, and that a single-key deployment behaves exactly as before.
3. **Startup checks.** Values are stripped and blank means unset, a read key alone refuses to start, equal values are allowed with a warning, and the warnings are logged once from the lifespan.
4. **Web client.** `web/src/api-keys.ts` (tested with `node --test`) and `web/src/admin-key-prompt.ts`: the prompt on a `403` naming the admin scope, per-request key selection, one shared prompt, a decline cooldown, and the Rules and Settings availability probes accepting `403`.
5. **CLI and tooling.** `--admin-api-key` / `DEVICE_MANAGER_ADMIN_API_KEY` for the remote REPL, a hint on `401` and `403`, and the new variables in the systemd units and `config/serve.py`.

## Rollout

Shipped in three PRs: the scope dependency, inventory test and startup checks (behaviorally identical when only `DOMESTI_API_KEY` is set); the web client; then the CLI flags, unit-file notes and documentation. Operators opt in by setting `DOMESTI_ADMIN_API_KEY` and, optionally, `DOMESTI_READ_API_KEY`; until they do nothing changes, and a test pins it. The admin key and the page-held control key must be different values: if they match, that value is granted admin, which is what the fallback already does.

## Rotation

Keys are independent. To rotate the control key, set a new `DOMESTI_API_KEY`, restart, and update whatever injects the meta tag. To rotate the admin key, set a new `DOMESTI_ADMIN_API_KEY` and restart; browsers re-prompt on the next `401`. Neither touches the Fernet key or stored secrets.

## Tests

- Backward compatibility: with only `DOMESTI_API_KEY` set, that key gets success on `/v1/settings/**`, `/v1/execute-line` and both sync routes with no prompt, and every existing API test passes unchanged.
- The inventory test and the key matrix above, including `GET /v1/meta` staying unauthenticated, `OPTIONS`/preflight needing no key, and the relay webhooks being untouched by the resolver.
- Keys: equal after strip, blank values, admin set with control unset (fail-closed), read set alone (startup error), header whitespace and case, non-ASCII and oversized values (all `401`, never `500`).
- Status codes: a wrong admin key on an admin route is `401`; a control key on an admin route is `403` with `required_scope`.
- Startup checks and their log lines, including the wildcard-bind warning treating any key as keyed.
- Web: the prompt flow (including the admin-only configuration, where the first protected request is preceded by the prompt and the entered key is sent everywhere), memory-only storage, the header sent only to admin routes, the probes on `403`, and that the Settings pages work unchanged when no admin key is configured.
- CLI: remote mode with only a control key prints the `403` message; with an admin key it succeeds.

## Decisions taken

1. Three scopes with `read` optional; a read key requires a control or admin key.
2. `POST /v1/execute-line`, all of `/v1/settings/**` and the two My Tracks sync routes require `admin`.
3. Public paths stay public, including `/v1/meta` and the schema pages.
4. An admin key alone is valid and fail-closed; equal values across variables are allowed with a warning.
5. Keys live in the environment only. The admin key reaches the browser by typing it into memory, not by embedding it in the page.
6. A mistyped key is `401` everywhere; `403` means a valid key with too little scope.

## Deferred

- Cookie sessions (HttpOnly, SameSite) with CSRF protection and a login page, which would replace the meta tag entirely.
- Hiding `/docs`, `/redoc` and `/openapi.json` when a key is configured.
- Multiple named keys per scope for audit trails, and rate limiting or lockout on repeated `401`.
