# Plan: multi-client UI consistency and real-time sync

This document tracks **remaining** planned work. Shipped items are summarized below with links; delete sections over time as the file goes stale.

---

## Shipped

| Item | PR(s) | Notes |
| --- | --- | --- |
| **SQLAlchemy persistence** | [#56](https://github.com/the-hcma/domesti-bot/pull/56) | `app/db/`, `kasa_discovery_store` facade, `app_secrets`, legacy `ALTER TABLE` |
| **Encrypted Tailwind token + desktop settings** | [#56](https://github.com/the-hcma/domesti-bot/pull/56)–[#60](https://github.com/the-hcma/domesti-bot/pull/60) | Fernet, ☰ Settings, `setup-secrets` REPL, hot-reload, dialog UX |
| **Compact mobile layout (viewport + saturated tiles)** | [#61](https://github.com/the-hcma/domesti-bot/pull/61)–[#65](https://github.com/the-hcma/domesti-bot/pull/65) | `COMPACT_LAYOUT_MQ`, 3-column grid, green/red/amber tiles; desktop saturated tiles with per-tile actions |
| **Compact tile UX polish** | [#66](https://github.com/the-hcma/domesti-bot/pull/66)–[#68](https://github.com/the-hcma/domesti-bot/pull/68) | Tile-only tap actions; inset exclude checkbox; 50/50 icon/label grid; Kasa lamp/room icons |
| **PWA shell refresh after deploy** | [#69](https://github.com/the-hcma/domesti-bot/pull/69) | `sw.js` stale-while-revalidate for `/` and `main.js` |
| **Compact tile label scale + layout tests** | [#74](https://github.com/the-hcma/domesti-bot/pull/74) | Fluid icon/label sizing; Playwright overflow checks at 320/390/768/1024/1440 |
| **Browser layout CI + dev deps** | [#75](https://github.com/the-hcma/domesti-bot/pull/75) | Parallel `Pytest (browser layout)` job; Playwright in `[dependency-groups] dev` |
| **Unified About dialog + PWA** | [#76](https://github.com/the-hcma/domesti-bot/pull/76) | Modal About (tagline, version, commit, copyright, GitHub); robot → About on **mobile** only; desktop ☰ → About; PWA v15+ |
| **Bulk action button color** | [#77](https://github.com/the-hcma/domesti-bot/pull/77) | `--bulk` / `btn-bulk` (orange) for global and per-family bulk-off; per-tile toggles stay red; PWA v18 |

### Web UI / compact tiles (reference)

- **Layout**: `data-layout="compact"` when viewport ≤ 768px (`web/src/main.ts`); styles in `app/api/static/index.html` (inline CSS, not the bundle).
- **Phone**: square tiles; top half icon, bottom half label (`line-clamp`); state shown by **color only** (no Playing/On text on compact).
- **Desktop**: same saturated tile chrome; tap toggles; **On/Off**, **Playing/Paused**, **Open/Closed** captions under labels.
- **Icons**: server resolves `UIDeviceOut.compact_icon` (object name beats room name beats Kasa model); client draws `garage_open` / `garage_closed` from live door state.
- **Bulk buttons**: warm orange (`--bulk`); per-tile off actions stay red (`--danger`).
- **About**: mobile robot icon or desktop ☰ menu; same modal content everywhere.
- **Deploy**: `git pull` then `setup-service` / `./scripts/on-deploy` rebuilds when inputs or `dist/main.js` drift. Installed PWAs need a new service worker (currently **`domesti-bot-pwa-v18`** in `sw.js`) or clear site data for shell HTML.
- **PWA user docs**: root `README.md` *Progressive Web App* section; `web/README.md` points to cache-bump workflow.

---

## 1. PyPI distribution (`pipx` / `pip install`)

### Goal

Ship **`domesti-bot`** on PyPI (`pipx install domesti-bot`) so users get `domesti-bot` and `domesti-bot-server` without a git checkout. **Release Please** on `main` bumps `pyproject.toml` version; merging the release PR tags the repo and CI publishes to PyPI.

### Version everywhere (CLI + server + UI)

All surfaces read **`app.build_info.get_build_info()`** (directly or via **`GET /v1/meta`**):

- **Web UI** — About dialog and bootstrap use `/v1/meta` (`version`, `commit`).
- **HTTP** — OpenAPI app `version` matches package version.
- **CLIs** — add **`--version`** on `domesti-bot` and `domesti-bot-server` (not implemented yet).

**PyPI / CI builds** run `scripts/embed_build_metadata.py` before `uv build` so wheels without `.git` still report the release version and tag SHA (`DOMESTI_EMBED_VERSION`, `DOMESTI_EMBED_COMMIT` → `app/_build_metadata.py`). Full resolution order and publish steps: **[`docs/PYPI_PUBLISH_PLAN.md`](PYPI_PUBLISH_PLAN.md)**.

### Checklist (summary)

1. Installable package (`[project.scripts]`, hatchling, static + `dist/main.js` in wheel).
2. CLI `--version` + embed step in release CI.
3. `.github/workflows/release-please.yml` + `uv publish` + smoke tests.
4. `docs/RELEASING.md` and first PyPI release.

---

## 2. Immediate cross-client perception (optimistic for everyone)

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

1. **PyPI / pipx release** — independent of UI sync; unlocks installs without a git checkout. See [`PYPI_PUBLISH_PLAN.md`](PYPI_PUBLISH_PLAN.md).
2. **Broadcast + cross-tab optimistic alignment** — largest architectural change; benefits most now that compact layout, About, bulk colors, settings, and deploy detection are done.

---

## Tracking

Delete or shrink sections in this file as work ships, or replace with links to ADRs / merged PRs. Prefer **one concern per PR** per repository rules.

When changing **compact** styles or **service worker** behavior, bump `VERSION` in `app/api/static/sw.js` whenever `index.html` or deploy-critical shell assets change, and document deploy steps in the PR test plan.
