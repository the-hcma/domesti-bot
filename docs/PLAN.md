# Plan: multi-client UI consistency and real-time sync

This document tracks **remaining** planned work. Shipped items are summarized below with links; delete sections over time as the file goes stale.

---

## Shipped

| Item | PR(s) | Notes |
| --- | --- | --- |
| **SQLAlchemy persistence** | [#56](https://github.com/the-hcma/domesti-bot/pull/56) | `app/db/`, `kasa_discovery_store` facade, `app_secrets`, legacy `ALTER TABLE` |
| **Encrypted Tailwind token + desktop settings** | [#56](https://github.com/the-hcma/domesti-bot/pull/56)–[#60](https://github.com/the-hcma/domesti-bot/pull/60) | Fernet, ☰ Settings, `setup-secrets` REPL, hot-reload, dialog UX |
| **Compact mobile tiles (3-column)** | (this branch) | `#app[data-layout="compact"]`, square tappable tiles, no exclude row |

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

## Suggested implementation order (remaining)

1. **Broadcast + cross-tab optimistic alignment** — largest architectural change; benefits most now that mobile compact layout and encrypted settings are done.

---

## Tracking

Delete or shrink sections in this file as work ships, or replace with links to ADRs / merged PRs. Prefer **one concern per PR** per repository rules.
