# Plan: multi-client UI consistency, Tailwind token from Web UI, compact mobile tiles

This document captures **planned** work (not implemented in the PR that introduces this file). Each stream should land in its **own worktree / branch** with focused reviews.

---

## 1. Immediate cross-client perception (optimistic for everyone)

### Goal

Whenever **any** user acts on a device (this browser, another phone, the REPL, another automation client), **every** connected Web UI session should **perceive** the change **immediately**, using the same mental model as today’s **self** actions: **assume the action succeeded**, update tiles optimistically, then **reconcile** when authoritative state arrives.

### Why

Today optimistic updates and pending predictions are centered on **the actor’s** tab. Other tabs or phones only learn the truth on the next **`GET /v1/ui/state`** poll (and hardware truth may lag behind **`DeviceStateWatcher`** polling). That feels inconsistent when multiple people watch the same dashboard.

### Direction (high level)

1. **Server-side source of truth** stays the in-memory managers + watchers; **`build_ui_state`** remains the canonical snapshot shape.
2. Introduce a **push channel** (WebSocket or SSE — see trade-offs below) from the server to browsers, authenticated the same way as the REST API (`DOMESTI_API_KEY` / future session model).
3. On **every successful mutating route** (per-device toggle, family bulk, global bulk, preference changes, etc.), **broadcast** a payload to all subscribers. Minimum viable payload: **“invalidate / refresh”** signal so clients call the existing `fetchState()` path; stronger variant: push a **full `UIStateOut` JSON** to skip an extra round-trip.
4. **Client** (`web/src/main.ts`): on push, merge into the existing **`pendingPredictions`** / optimistic machinery so **remote** actions get the same UX as **local** clicks (or explicitly mark origin so UI is identical without duplicating logic).
5. **Ordering / races**: define rules when a broadcast arrives **before** the actor’s HTTP response (unlikely but possible with parallel clients) — version counter or monotonic `revision` on `UIStateOut` if needed.

### Dependencies / risks

- **Hardware latency** unchanged: if the garage relay is slow, the tile may still flip back after reconciliation — same as today for the acting user.
- **Auth for WebSocket**: browsers cannot always attach custom headers; plan for **query token**, **first-message auth**, or **short-lived ticket** issued via `POST` — document threat model (logs, Referer, TLS).
- **Reverse proxies** must allow WebSocket upgrade or chunked SSE.
- **Tests**: ASGI WebSocket tests with `httpx` / Starlette test client; hermetic, no LAN.

### Related reading

- `app/device_state_watcher.py` — server poll cadence for **out-of-band** hardware changes (separate from push latency).
- Prior discussion: tightening **`DOMESTI_STATE_POLL_INTERVAL_S`** is a cheap complement; push addresses **multi-session** consistency more than shaving the last second off hardware polls.

---

## 2. Desktop Web UI: set `TAILWIND_TOKEN` with safer persistence

### Goal

When running the dashboard from a **computer** (non-mobile or dedicated “settings” surface — exact UX TBD), allow the operator to **paste or rotate** the GoTailwind API token **without** SSH-only configuration, and persist it so the server survives restarts **more securely than a plain-text row in an unencrypted SQLite file**.

### FastAPI / ecosystem note

**FastAPI does not ship an application secret vault.** Patterns in the ecosystem:

| Approach | Notes |
| --- | --- |
| **Environment / `EnvironmentFile=`** (current production default) | Still the strongest default: OS file permissions, no DB at rest. |
| **Encrypt at rest** (e.g. `cryptography.fernet`) | Single master key in **`DOMESTI_SECRETS_KEY`** (32-byte url-safe base64) or OS keychain; store ciphertext + metadata in SQLite. Key rotation is a follow-up project. |
| **OS secret store** (Keychain, Secret Service) | Possible on dedicated home-server Linux with user session; less portable in containers. |
| **External vault** (HashiCorp Vault, cloud KMS) | Likely overkill for this repo’s scope unless requirements grow. |

### Direction (high level)

1. **Settings** route or modal (desktop-only or gated behind “advanced”) collecting the token once; **never** echo it back in API JSON; **never** log it.
2. **Persistence API**: new encrypted column or sidecar table `app_secrets` with `key='tailwind_token'`, ciphertext bytes, `updated_at`.
3. **Runtime read path**: lifespan or `GotailwindDeviceManager` factory reads decrypted value into memory only; disk stays ciphertext.
4. **Migration / bootstrap**: if env `TAILWIND_TOKEN` is set, it wins over DB until explicitly migrated; document precedence.
5. **Threat model** in the implementing PR: who can hit the endpoint, CSRF if cookies ever added, TLS requirement on non-loopback.

### Dependencies

- Likely **`cryptography`** dependency via `uv add` with lockfile update.
- **Pyright + tests** for encrypt/decrypt round-trip and handler redaction.

---

## 3. Compact mobile layout (fixed tiles)

### Goal

On **mobile** form factor, offer a **denser** dashboard: **fixed-size square tiles** in a **two-column grid** (or responsive breakpoints), each tile showing:

- **Device name** (primary label).
- **On / off (or open / closed / playing / paused)** as the dominant visual state **inside** the tile (color and/or icon — TBD for accessibility).
- **No** per-tile action verb (“Turn it off”) as the main label — the whole tile remains tappable to toggle where applicable.
- **No** “exclude from …” row on mobile (advanced preference stays on desktop / expandable panel / settings).

### Design reference

See **`docs/figures/mobile-compact-tile-reference.png`** — external product mock for **layout density and grid rhythm** (large color blocks, centered icon, short caption). domesti-bot will adapt colors to existing **`--family-color`** tokens and state semantics rather than copying unrelated iconography.

### Direction (high level)

1. **`isMobileFormFactor()`** (or CSS `@media` + `matchMedia`) chooses **layout mode**: `compact` vs `comfortable`.
2. New CSS under `app/api/static/index.html` (or split CSS if we outgrow the single file) for the grid, tile min sizes, and touch targets (minimum ~44px).
3. **`renderDevice`** (or parallel `renderDeviceCompact`) branches; keep one data path to `api` / controller methods.
4. **Accessibility**: `aria-pressed`, focus rings, reduced-motion path.
5. **Tests**: optional Playwright later; for now unit-free but **manual test matrix** in the implementing PR (iOS Safari, Android Chrome).

---

## 4. SQLAlchemy for persistence (in progress)

### Goal

Replace raw ``sqlite3`` calls in :mod:`app.kasa_discovery_store` with **SQLAlchemy 2.x** ORM access so schema, migrations, and new tables (e.g. ``app_secrets``) share one code path.

### Direction

1. ``app/db/`` — ``Base``, ORM models, ``bootstrap_schema``, legacy ``ALTER TABLE`` steps, ``discovery_session``.
2. Keep :mod:`app.kasa_discovery_store` as the **public facade** (call sites unchanged).
3. Add ``app_secrets`` for Fernet-encrypted values (Tailwind token first).
4. Hermetic tests for secrets + settings routes; existing discovery-store tests must stay green.

---

## Suggested implementation order

1. **Compact mobile tiles** — isolated UI/CSS/TS change; lowest coupling; immediate user-visible win.
2. **Encrypted Tailwind token storage + settings UI** + **SQLAlchemy migration** (this branch) — security review; touches lifespan, DB, API schemas.
3. **Broadcast + cross-tab optimistic alignment** — largest architectural change; benefits most once (1) and (2) stabilize.

---

## Tracking

Delete or shrink sections in this file as work ships, or replace with links to ADRs / merged PRs. Prefer **one concern per PR** per repository rules.
