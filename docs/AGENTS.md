# AGENTS.md — Ground Rules for domesti-bot

This file defines the non-negotiable standards for all contributors (human or AI) working on this codebase. Every change must comply with these rules before it is considered complete.

---

## Session Startup

- **At the start of every session, before any other work**, run both commands in order ([repository-helpers](https://github.com/the-hcma/repository-helpers)):
  ```
  ~/work/ai/repository-helpers/scripts/dev/start-development --refresh
  ~/work/ai/repository-helpers/scripts/dev/start-development
  ```
  - **`--refresh`** (first): syncs `main` with Graphite (`gt sync`), prunes merged worktrees and branches, pulls latest `main`, and ensures the systemd user unit (`domesti-bot.service` from `etc/systemd/`) is installed and running via `setup-service`. Exits immediately — it does **not** prompt for a worktree.
  - **plain** (second): repeats the sync/cleanup, then prompts you to name a new worktree for the upcoming work. Pass `--worktree <name> --no-interactive` to skip the prompt.
- Both commands are required. This replaces any manual `gt sync --force` step.
- After `start-development` finishes, **`cd` into the stack worktree** (`.worktrees/<stack-name>-wt`) before any other work. Do not stay in the primary clone.

### Main worktree is off-limits (agents)

The **primary clone** (repo root — first entry in `git worktree list`, usually on branch `main`) is the **main worktree**. Treat it as **read-only** unless the user explicitly authorizes touching it in the current conversation.

**Never on the main worktree** (without explicit user authorization):

- Edit, create, or delete source files, config, or lockfiles
- Run `uv sync`, tests, builds, or formatters
- Run `dep-updater` with `--dir` pointing at the primary clone (it may fast-forward `main` and mutate git state)
- Run `gt create`, `gt modify`, `gt submit`, `gt sync`, `gt restack`, or other Graphite/git write operations
- Leave uncommitted changes, stray branches, or detached HEAD state

**Always** do implementation, investigation that mutates state, and validation in a **stack worktree** under `.worktrees/<stack-name>-wt`. Pass that path to tools (`--dir`, `cd`, etc.).

`start-development` may update the main worktree for environment sync only; that is not permission to work there. If you need to inspect `main` without changing it, use read-only commands (`git log`, `git show`, `gh pr view`) or a **detached temporary worktree** — not the primary clone.

---

## Language & Runtime

- **Python 3.14** is the target runtime (`.python-version`). `pyproject.toml` declares `requires-python = ">=3.12"` as the compatibility floor.
- Use **modern Python typing** — built-in generics, not the `typing` module equivalents:
  - `list[str]`, `dict[str, Any]`, `tuple[str, ...]`, `set[int]`, `str | None`
  - ❌ `List[str]`, `Dict[str, Any]`, `Optional[str]`
  - Only import from `typing` what you actually need (`Any`, `Annotated`, `cast`, `TypeVar`, `TYPE_CHECKING`).
- Every new module starts with `from __future__ import annotations` (matches the rest of the codebase).
- Every public function and method has complete type annotations (parameters and return type). `pyright` enforces this.

---

## Package Management

- **Only `uv`** is used for Python package management. Never `pip` directly.
- **Production vs dev dependency groups**:
  - **`[project] dependencies`** — runtime only (FastAPI, device libraries, uvicorn, …). Used by `scripts/on-deploy` (`uv sync --frozen`), production systemd installs, and a plain `uv sync` when you want a deploy-shaped venv.
  - **`[dependency-groups] dev`** — contributors and CI only: `pyright`, `pytest`, `pytest-asyncio`, `pytest-xdist`, `playwright`, `icecream`. Install with `uv sync --group dev` (or `uv sync --all-groups`).
  - Add runtime packages: `uv add <pkg>`. Add tooling: `uv add --group dev <pkg>`. Do not hand-edit `pyproject.toml` for additions.
- **Contributors** should run `uv sync --group dev` after clone or pull when `pyproject.toml` / `uv.lock` change. Pyright and pytest are not installed by `uv sync` alone.
- The lock file (`uv.lock`) **must always be committed**.
- Prefer well-maintained, typed packages. Do not add a dependency for something trivially implementable in ~10 lines of Python.

---

## Project Layout

```
domesti-bot/
├── app/                                  Domain code (one package, flat-ish)
│   ├── __init__.py
│   ├── androidtv_device_manager.py        Google Cast (pychromecast)
│   ├── device_manager.py                  Shared base classes for *DeviceManager
│   ├── domesti_bot_cli.py                 prompt_toolkit REPL + argparse wiring
│   ├── gotailwind_device_manager.py       GoTailwind garage doors
│   ├── kasa_device_manager.py             TP-Link Kasa / Tapo (python-kasa)
│   ├── device_discovery_store.py            SQLite discovery cache (shared by all)
│   ├── rule_engine.py                     Device base types, geofences, actions
│   ├── sonos_device_manager.py            Sonos zones (soco)
│   └── api/                              FastAPI HTTP surface (subpackage)
│       ├── __init__.py
│       ├── app.py                         create_app(args) + endpoint definitions
│       └── schemas.py                     Pydantic request/response models
├── config/                               Process-level wiring
│   ├── __init__.py
│   └── serve.py                           uvicorn entrypoint (run via `python -m config.serve`)
├── tests/
│   ├── python/                           All pytest tests
│   │   ├── test_*.py
│   │   └── fixtures/                      Static fixture trees (e.g. androidtv/)
│   └── bash/                             Reserved for shell-script tests
├── scripts/                              Dev / runtime entrypoints (no `.sh`)
│   ├── domesti-bot                        `uv run python -m app.domesti_bot_cli "$@"`
│   ├── domesti-bot-server                 `uv run python -m config.serve "$@"`
│   ├── on-deploy                          setup-service build hook (exit 0/1/2+ contract)
│   └── verify_google_cast_discovery.py    standalone discovery probe
├── etc/systemd/                          User unit template for ``setup-service`` (@@REPO_DIR@@)
│   └── domesti-bot.service
├── production/                           Server-side deploy bits (optional **system** unit)
│   └── systemd/domesti-bot-server.service.template
├── web/                                  Browser TypeScript bundle (see "Web UI" below)
│   ├── package.json                       pnpm scripts; pinned via `packageManager`
│   ├── pnpm-lock.yaml                     committed; `pnpm install --frozen-lockfile`
│   ├── tsconfig.json                      strict TS + Bundler module resolution
│   ├── build.mjs                          one esbuild call → app/api/static/dist/main.js
│   └── src/main.ts                        browser entrypoint
├── app/api/static/                       Files served at `/static/` by FastAPI
│   ├── index.html                         landing page (loads /static/dist/main.js)
│   ├── manifest.webmanifest               PWA manifest (icons + ``display: standalone``)
│   ├── sw.js                              service worker (also routed at ``GET /sw.js`` for ``scope: /``)
│   ├── icons/                             committed PNG launcher icons
│   └── dist/                              `pnpm run build` output (gitignored)
├── AGENTS.md → docs/AGENTS.md            Symlink so Cursor / agent tools auto-discover at root
├── docs/
│   ├── AGENTS.md                         (this file — canonical location)
│   └── GRAPHITE.md                        Forward reference; not yet authored
├── .github/workflows/                    See "CI Checks" below
├── pyproject.toml                        Deps, pytest config, tool config
├── pyrightconfig.json                    pyright include/exclude + Python version
└── uv.lock
```

**Layout rules**:

- All Python source lives in `app/`. Sibling imports inside `app/` use **absolute** form: `from app.rule_engine import Device` — not relative (`from .rule_engine import Device`).
- The HTTP API is a subpackage `app/api/`. Do not flatten Pydantic schemas into `app/` proper.
- `config/` holds only process-level wiring (uvicorn launch, future settings glue). It is **not** a place for domain code.
- **Never move** `pyproject.toml`, `pyrightconfig.json`, `uv.lock`, or `.python-version` out of the repo root — IDE and tool discovery walk up from source files and depend on root-level placement.
- Tests live in `tests/python/`. Real-hardware integration tests live alongside unit tests but carry the `@pytest.mark.integration` marker. Static fixture data lives in `tests/python/fixtures/`.
- `tests/bash/` is reserved for future shell-script tests. Keep `.gitkeep` until real tests exist.
- **Browser code lives in `web/`** (TypeScript). Sources never go in `app/api/static/` — only the build output (`app/api/static/dist/`) does, and that is gitignored. Other committed files under `app/api/static/` (HTML, PWA assets) are not TypeScript sources. See "Web UI" below.

---

## Code Style

- **Lexicographic code organization** — enforced by `.cursor/rules/python-sorted-methods.mdc`. **Public** module-level names and class members first (constants, classes, functions, properties) sorted ASCII lexicographically within that block; **private** (`_`-prefixed) module helpers and class members second, sorted within that block. Do not append new APIs at the bottom — insert in sorted position. Dunder methods sit in the public class block. Same public-then-private lexicographic principle applies broadly when organizing code.
- **Imports must be at module level.** No lazy / local imports inside functions or methods. `TYPE_CHECKING` guards are acceptable (they are module-level by nature).
- **Import order** follows PEP 8 (standard library, third-party, local) and matches what already exists in the repo.
- **Empty lines must contain no whitespace** (no trailing spaces/tabs).
- **Error messages** must provide context. Format: "Expected <type/constraint>, got <actual_value>". ❌ `"Invalid input"` ✅ `"Expected a sequence, got int"`.
- **Spelling locale**: American English in all new project-authored prose (docstrings, comments, user-visible API field descriptions, README sections this repo maintains). Do not rewrite third-party literals (e.g. upstream API field names) when those spellings are required for correctness.
- **Module-level mutable state**: avoid the `global` keyword. Group related mutable state into a holder class and expose one module-level instance.
- **Nomenclature for mappings**: use `{key}_to_{value}` instead of generic `_map` / `_dict` suffixes (e.g. `host_to_label`, not `label_map`).
- **Prefer `with` (context managers) over `try / finally`.** Any time a resource has a cleanup action that must run on both the success and failure path, the resource should be acquired with a `with` (or `async with`) block — not a hand-rolled `try / finally`. Examples in this codebase:
  - `sqlite3.Connection` → `with contextlib.closing(sqlite3.connect(path)) as conn:` (the bare `with sqlite3.connect(...) as conn:` form only wraps a transaction; it does **not** close the connection, so always use `contextlib.closing`).
  - `socket.socket` → `with bind_listen_socket(host, port) as sock:` (sockets are native context managers).
  - `app.device_manager.DeviceManager` (and every per-family `*DeviceManager` subclass) → `async with KasaDeviceManager(...) as mgr: await mgr.fetch(); ...`. `__aexit__` calls `disconnect()` so callers no longer need an explicit `try / finally`.
  - When wrapping a one-shot teardown of a third-party object that exposes no `__exit__` / `__aexit__`, write a small `@contextlib.contextmanager` / `@contextlib.asynccontextmanager` helper at module scope rather than inlining the same `try / finally` in every call-site.
  - **Acceptable exceptions** (do not rewrite these as context managers):
    - A `try / finally` that is itself the body of an `@asynccontextmanager` (e.g. the FastAPI lifespan in `app.api.app.create_app` — that `finally` is the cleanup half of the context manager being defined).
    - A `try / finally` that restores **scalar state** rather than releasing a resource (e.g. `SonosDeviceManager.rediscover` saves and restores `self._force_discovery`). Inline `try / finally` is fine for a 2–3 line state toggle.
    - Third-party objects that only expose imperative cleanup methods (no `__exit__` / `__aexit__`) **and** are used in exactly one call-site (e.g. `pychromecast.CastBrowser.stop_discovery()` in `app/androidtv_device_manager.py`). Add a wrapping context manager when the same pattern repeats in three or more call-sites.
- **HTTP status codes use `http.HTTPStatus`, not integer literals.** Server code (`HTTPException(status_code=HTTPStatus.NOT_FOUND, ...)`, `Response(status_code=HTTPStatus.NO_CONTENT)`), HTTP clients (`if response.status_code == HTTPStatus.ACCEPTED`), and tests (`assert response.status_code == HTTPStatus.OK`) all reference the named constants. `HTTPStatus` is an `IntEnum`, so it compares equal to the integer codes — wire format is unchanged. Common values used in this repo: `OK` (200), `ACCEPTED` (202), `NO_CONTENT` (204), `BAD_REQUEST` (400), `UNAUTHORIZED` (401), `FORBIDDEN` (403), `NOT_FOUND` (404), `CONFLICT` (409), `UNPROCESSABLE_ENTITY` (422), `INTERNAL_SERVER_ERROR` (500), `SERVICE_UNAVAILABLE` (503). Use `response.status_code >= HTTPStatus.BAD_REQUEST` (not `>= 400`) for error buckets.

---

## Type Checking

- **`pyright`** is the type checker. Run before every commit:
  ```
  uv run pyright
  ```
- No new code may introduce type errors. Existing files must not regress.
- Do not silence type errors with `# type: ignore` unless absolutely unavoidable; every suppression needs a comment explaining why.

---

## Testing

- **`pytest`** is the framework. Configuration lives in the `[tool.pytest.ini_options]` block of `pyproject.toml`:
  - `testpaths = ["tests/python"]` — pytest discovery is scoped to the canonical test root.
  - `asyncio_mode = "auto"` — `async def test_*` works without `@pytest.mark.asyncio`. Continue using the explicit decorator for clarity in existing files where it is already present.
  - **Parallel hermetic runs** — CI and the pre-PR gate use **`pytest-xdist`** with **`-n auto`** so the hermetic suite spreads across CPU cores (`uv run pytest -m "not integration and not browser" -n auto`). Omit **`-n auto`** when you need a single process (e.g. `pdb`). New tests must stay **process-safe**: no fixed listen ports, no reliance on a shared mutable module global without a lock, no accidental dependence on collection order across workers.
  - **Browser layout tests** (`@pytest.mark.browser`) — headless Chromium via **Playwright** checks that long device labels stay inside compact/comfortable tiles using production CSS from `app/api/static/index.html`. They are **not** run under xdist (module-scoped browser fixture). CI runs them in a **separate parallel job** (`Pytest (browser layout)` in `ci.yml`). Locally (after `uv sync --group dev` and a one-time `uv run playwright install chromium`):
    ```
    uv run pytest tests/python/test_landing_compact_tile_label.py -m browser -v
    ```
    Without the dev group or Chromium install, browser tests are **skipped** (`pytest.importorskip`), not failed. The CSS contract test in the same file runs without Playwright.
  - Tests import application code via the full package path: `from app.kasa_device_manager import KasaDeviceManager` (never `from kasa_device_manager import ...`).
  - Mock patch targets follow the same rule: `patch("app.androidtv_device_manager._discover_cast_infos_sync", ...)` — patching by the symbol's defining module, with the full dotted path.
- **Integration tests** that exercise real LAN hardware are marked `@pytest.mark.integration` and skipped by default in CI/local quick runs. Document required env vars (e.g. `KASA_USERNAME`, `TAILWIND_TOKEN`) at the top of the test file.
- **Unit tests must be deterministic and hermetic**:
  - No real network I/O (`pychromecast.discover_chromecasts`, kasa discovery, etc. — patch via `unittest.mock`).
  - No real file I/O outside `tmp_path` fixtures.
  - No `Math.random()`-style nondeterminism; no un-mocked `time.time()` / `datetime.now()` in assertions.
- **No fixed-delay sleeps in tests** (`time.sleep(0.5)`, `await asyncio.sleep(0.5)`). They are a flake smell — use condition-based synchronization (event objects, polling with a deadline, observable state transitions) instead.
- **No hardcoded well-known ports** in tests. Use port `0` and let the OS allocate. Assert against the allocated port when needed, not against the well-known one.
- **Mock data must match real-world values.** Before mocking `sys.argv`, Cast `CastInfo`, kasa device configs, etc., verify what the real values look like. Add guard assertions when in doubt.
- **Test naming**: `test_<behavior>_<condition>` reads as a sentence (e.g. `test_fetch_skips_mdns_when_sqlite_cache_fully_named`).
- Each test asserts an **observable outcome** — not merely that a mock was called.

Run the suite (requires `uv sync --group dev`):
```
uv run pytest -m "not integration and not browser" -n auto   # hermetic, mirrors CI (parallel)
uv run pytest -m "not integration and not browser"          # hermetic, single-process (pdb)
uv run pytest -m "browser"                                  # Playwright layout (single process)
uv run pytest -m "not integration" -n auto                  # hermetic + browser (browser not under xdist)
uv run pytest                                                 # full suite
uv run pytest -m integration                                  # LAN hardware only
```

---

## HTTP API

- The FastAPI app is created via `app.api.app.create_app(args)`; the entrypoint is `config/serve.py` (run as `python -m config.serve`, or via `scripts/domesti-bot-server`).
- **Authentication**: when `DOMESTI_API_KEY` is set in the environment, every protected endpoint requires the `X-Domesti-Api-Key` header. If the env var is unset, the API is open (intended for trusted LAN only — never expose unauthenticated to the public internet).
- **Bind address**: the **user** unit (`etc/systemd/domesti-bot.service`, fpdf-style) passes `--listen-all --listen-port 8003` so the API is reachable on the LAN when no reverse proxy runs on the same host—set `DOMESTI_API_KEY` whenever clients are not fully trusted. The **system** template (`production/systemd/domesti-bot-server.service.template`) keeps `127.0.0.1:8003` for nginx-on-the-same-box deployments.
- **Dev-mode default** (no flags, no env vars): bind to `127.0.0.1` on an **OS-allocated free port** so local collisions with other listeners are unlikely. The startup banner logs `[http] listening on http://127.0.0.1:<port> (api-key …)` so the developer can paste the URL into a browser. The launcher pre-binds the socket with `config.serve.bind_listen_socket()` *before* lifespan / device discovery runs, so the URL appears at the top of the run rather than after the discovery wait. Use `--listen-port <port>` or `DOMESTI_LISTEN_PORT=<port>` to pin a specific port. Precedence: CLI flag → env var → dev default.
- **CORS**: the dev configuration uses `allow_origins=["*"]`. Tighten this before exposing the service outside the LAN.
- **Pydantic schemas** for all request and response bodies live in `app/api/schemas.py`. New endpoints must define typed `*In` / `*Out` models — no raw `dict[str, Any]` return types.
- **`GET /v1/meta` and build stamps.** The web UI reads package version and source commit via :func:`app.build_info.get_build_info` (also used for the FastAPI/OpenAPI `version` string). Resolution order: ``GITHUB_SHA`` or ``DOMESTI_GIT_COMMIT`` when set; then constants in :mod:`app._build_metadata` (populate before packaging with ``./scripts/embed_build_metadata.py`` and ``DOMESTI_EMBED_VERSION`` / ``DOMESTI_EMBED_COMMIT`` so PyPI installs and shallow containers without ``.git`` still show accurate metadata); then ``importlib.metadata.version("domesti-bot")`` after ``pip install``; then ``pyproject.toml`` for a source checkout; finally ``git rev-parse`` when a repository is present.
- **Endpoint additions** must:
  - declare `dependencies=[Depends(_verify_api_key)]` (or be intentionally public like `/health`).
  - return a Pydantic model, not a `dict`.
  - have at least one unit test that drives the route via `httpx.AsyncClient` against the ASGI app.
- **Deferred-discovery lifespan (HTTP server is ready before discovery finishes).** The FastAPI lifespan in `app.api.app.create_app` is non-blocking:
  - On startup it sets `app.state.device_state = None`, `app.state.discovery_error = None`, schedules `bootstrap_device_managers(...)` as an `asyncio` task named `device-discovery`, and **yields immediately**. The ASGI `Application startup complete` log line therefore appears within milliseconds of binding the socket — not after a 5–35s LAN sweep.
  - When discovery succeeds the task assigns `app.state.device_state` and logs `[startup] device discovery complete in N.Ns`. If it raises, the exception's `repr()` is stored in `app.state.discovery_error` and logged.
  - On shutdown the task is cancelled (if still in flight), then `shutdown_device_managers` is called on whatever state did materialize.
  - Static routes (`/`, `/favicon.ico`, `/health`) MUST NOT depend on `app.state.device_state`; they are designed to work the instant the lifespan yields.
  - Routes that need the device managers MUST depend on `_device_state` (or take `DeviceState` annotation). `_device_state` returns a `503 Service Unavailable` with a `Retry-After` header until discovery completes:
    - in-progress → `Retry-After: 2` and `detail: "Device discovery still in progress; check /health and retry shortly"`
    - failed → `Retry-After: 30` and `detail: "Device discovery failed: <repr(exception)>"`
  - `/health` reports the discovery state in its payload: `{"status": "ok", "service": "domesti-bot", "ready": bool, "discovery": "in_progress" | "ready" | "failed", "error": str | None}`. Programmatic readiness checks (smoke tests, deploy hooks, integration tests) should poll `/health` for `discovery == "ready"` rather than racing on the listen banner.
  - **Do not** re-introduce blocking work into the lifespan. Long-running setup (LAN probes, file rebuilds, cache warming) belongs in `_run_discovery` or a similar background task, gated behind a `app.state.*` readiness flag.
- **Landing page.** `GET /` reads `app/api/static/index.html` from disk on every request (so dev edits show up without restarting the server) and returns it as `HTMLResponse`, `include_in_schema=False`. The HTML is intentionally minimal: a `<title>` and an empty `<div id="app">` that the compiled TypeScript bundle (`/static/dist/main.js`) hydrates with the tile UI. No admin chrome (success banner, endpoint listing) appears on the user-facing page — endpoint discovery lives in `/openapi.json`. The page is safe to hit before discovery completes (no `_device_state` dependency).
- **Spinner during initial discovery (no 503 banner on first paint).** `/v1/ui/state` returns 503 with `detail: "Device discovery still in progress; ..."` and `Retry-After: 2` while the lifespan's background discovery is running. The frontend's `DomestiBotController.bootstrap()` recognizes that specific 503 (via `HttpError.isDiscoveryInProgress()`, which parses the `detail` field) and keeps a CSS-only spinner up instead of rendering the error banner. It re-polls every `BOOTSTRAP_RETRY_MS = 2s` (matches the server's `Retry-After` hint) until one of three things happens: a payload arrives → tiles render; a different error arrives (auth, 500, network, or 503 with `detail: "Device discovery failed: ..."`) → the error banner replaces the spinner so a wedged or misconfigured server is still visible; the wall-clock deadline `BOOTSTRAP_DEADLINE_MS = 90s` elapses → the banner shows "Device discovery is taking longer than expected" so a healthy-looking spinner can't hide a stuck server forever. Post-bootstrap polling (`POLL_MS = 5s`) is unchanged — transient 503s after that point just flip the family frames to red until the next successful poll.
- **Continuous state monitoring.** Each device manager has a backing `DeviceStateWatcher` in `app/device_state_watcher.py` that runs forever in the background and keeps the in-memory `is_on` / `is_playing` / `is_open` flags fresh. The lifespan starts watchers right after discovery succeeds and signals them via `app.state.watcher_stop` (an `asyncio.Event`) **before** tearing down managers on shutdown, so we never poll a half-disconnected backend. Default cadence is `DEFAULT_POLL_INTERVAL_S = 10s`; override with `DOMESTI_STATE_POLL_INTERVAL_S` (positive float, must be ≥ 1.0). **One asyncio task per backend** runs in parallel; **within each family every device is polled concurrently** each cycle via `_refresh_all_devices_concurrently`. New families must ship async manager refresh APIs (blocking I/O via `asyncio.to_thread`) and a `*PollingWatcher` — see `.cursor/rules/device-family-async-polling.mdc`. Current backends ship polling implementations (no upstream library exposes a webhook / event stream today); a future event-driven backend should ship its own `DeviceStateWatcher` subclass — the lifespan picks it up through `build_default_watchers` without any other change. Per-device exceptions inside a watcher are logged and swallowed; one bad device must not stop the loop.
- **Auto-open browser (dev mode).** When `scripts/domesti-bot-server` is launched interactively and binds to a loopback address (`127.0.0.1` / `::1`), `config.serve.browser_url_for_auto_open` returns `http://127.0.0.1:<port>/` and `_open_browser_after_server_ready` waits for `server.started=True` then calls `webbrowser.open(url, new=2)`. Auto-open is suppressed when (a) `--no-browser` is passed, (b) `$INVOCATION_ID` is set (we're running under a systemd unit), or (c) the bind is on `0.0.0.0` / a specific LAN IP. The production systemd template passes `--no-browser` explicitly so the intent is visible in the unit file. Browser-side failures (no `$DISPLAY`, no `webbrowser` registered) are logged and swallowed — never take the launcher down.
- **LAN-accessible mode (`--listen-all`).** `scripts/domesti-bot-server --listen-all` is a convenience shortcut for `--listen-host 0.0.0.0` — useful for opening the UI on a phone or another laptop on the same network. Precedence inside `resolve_listen_address`: explicit `--listen-host ADDR` always wins, then `--listen-all` → `0.0.0.0`, then `$DOMESTI_LISTEN_HOST`, then the dev default `127.0.0.1`. The banner enumerates every non-loopback IPv4 the host knows about (one `[http] network: http://…` line each). Browser auto-open stays suppressed on wildcard binds (no single "right" URL to pick). **Security nudge**: when bound to `0.0.0.0` *and* `$DOMESTI_API_KEY` is unset, the launcher logs a single `WARNING` — every LAN client can otherwise drive every endpoint unauthenticated. The **user** systemd unit (`etc/systemd/domesti-bot.service`) passes `--listen-all` for the same reason fpdf does; the **system** template under `production/systemd/` keeps loopback when TLS terminates on the same machine.
- **Optimistic UI updates (per-tile actions).** Clicks on a tile's `Turn it on/off` or `Open it / Close it` button mutate the controller's cached `state` *before* the HTTP round-trip via `predictDeviceState(family_id, device_id, next_state)` and re-render. The button label flips immediately so the user can chain actions without waiting for the response. The prediction also registers a `PendingPrediction` entry with `expiresAt = performance.now() + OPTIMISTIC_GRACE_MS` (8s) keyed by `familyId\u0000deviceId`. On every subsequent `refresh()` we run `applyPendingPredictionsTo(state)`: matching readings drop the pending entry (confirmed), contradicting readings *during the grace window* are overlaid with the predicted state (no label flicker while a Tailwind door transitions through `unknown`/`OPENING` or a Kasa relay is still settling), and expired entries are dropped so the next refresh shows reality (this is how a genuinely failed action becomes visible after ~8s). On per-tile action failure (HTTP throw) we drop the prediction immediately and refresh — the action provably didn't land, so the grace window would only mislead.
- **Optimistic UI updates (bulk actions).** The per-family `Turn off all` / `Pause all` / `Close all` and the global `Turn off / pause / close everything` buttons use the same prediction machinery via `predictBulkOffForFamily(family_id)` / `predictBulkOffGlobal()`. Both helpers walk the cached state, skip every device with `exclude_from_global=true` (the backend won't touch them, so neither should the overlay), and call `predictDeviceState(..., bulkOffStateForKind(device.kind))` for each remaining device — `"off"` for switches, `"paused"` for speakers, `"closed"` for doors. The grace window then suppresses any contradicting poll reading until the bulk action is confirmed or the 8s window expires. On bulk-dispatch failure we clear the just-registered predictions (family-scoped for `onBulkOffFamily`, everything for `onBulkOffGlobal`) so a request that never landed server-side doesn't leave the UI lying about device state.
- **Standardized state + action colors.** CSS variables drive every state / action color on the landing page: `--accent` (green) for active / positive outcomes, `--danger` (red) for single-device off/pause/close actions, `--bulk` / `--bulk-fg` (warm orange + white label, `btn-bulk`, tuned separately in light and dark theme blocks) for per-family and global bulk-off controls, and `--pending` (amber) for transient `unknown` state (Tailwind OPENING/CLOSING, Sonos pre-poll). **State badges**: `tile-state-on`, `tile-state-playing`, and `tile-state-open` are green; `tile-state-off`, `tile-state-paused`, and `tile-state-closed` are red; `tile-state-unknown` is amber. **Per-tile toggle**: colored by the *action* it will perform, not the current state — `data-on="false"` (next click turns the device on, resumes playback, or opens it) is green, `data-on="true"` (next click turns it off, pauses it, or closes it) is red. **Bulk buttons**: per-family `Turn off all` / `Pause all` / `Close all` and global `Turn off / pause / close everything` use `btn-bulk` (`--bulk`) so multi-device commands are visually distinct from per-tile red toggles while still reading as cautionary. When introducing a new button, pick the variable that matches the *outcome* of clicking it; do not reach for `--family-color` (that variable owns the family-grouping cues — the left tile stripe and the icon next to the family heading — and must not double-duty as state).
- **Disable actions when backend is unreachable.** When `refresh()` fails and `connected` flips to `false`, every action element is rendered with the HTML `disabled` attribute: per-tile toggle, per-tile "exclude" checkbox, per-family bulk button, and the global "Turn off / pause / close everything" button. CSS rules (`.tile-toggle:disabled, .btn:disabled`) gray-scale them and set `cursor: not-allowed`; the red family frame plus the grayed controls together make the offline state unmistakable. As soon as the next poll succeeds the controls re-enable on the next render — no extra plumbing needed.
- **Sonos zones in the web UI.** The `sonos` family renders alongside `kasa` and `tailwind` in alphabetical order. `UIDeviceOut.kind = "speaker"` and `state = "playing" | "paused" | "unknown"`. Per-tile clicks call `POST /v1/ui/sonos/zones/{uid}/toggle` (body: `{"playing": bool}`) which dispatches to `SonosSpeakerDevice.pause()` / `resume()`; the per-family `Pause all` button calls `POST /v1/ui/sonos/pause-all` (skips zones whose cached `is_playing` is not `True`); and the global `Turn off / pause / close everything` action calls `_bulk_pause_sonos_apply_impl` after the kasa and before the tailwind step. The `unknown` state covers the pre-poll window plus any zone whose UPnP transport read failed; the controller treats it the same way it treats `paused` for the toggle (so the next click pauses — a SoCo `pause` on a stopped queue is a no-op rather than an error). Read state from `SonosSpeakerDevice.is_playing` (`bool | None` cache), never from `transport_state_summary()` on the request path — the latter is a live UPnP call and would block the HTTP worker on a slow zone. The cache is refreshed by `SonosPollingWatcher` every `DEFAULT_POLL_INTERVAL_S` seconds and by every `pause()` / `resume()` call (post-action sync). Family color: `#8B5CF6` (violet) — picked so kasa-blue, sonos-violet, and tailwind-green are all distinguishable on a single screen.
- **Sonos UPnP 701 ("Transition not available").** Sonos rejects a `Play` on a zone with an empty queue / no media source, and rejects either `Play` or `Pause` while a zone is mid-`TRANSITIONING`. `SonosSpeakerDevice.pause()` / `resume()` catch the resulting `SoCoUPnPException(error_code="701")`, refresh the cached `is_playing` from a live UPnP read (so the next render shows truth), and re-raise as `SonosTransitionUnavailableError`. The HTTP endpoint maps that domain error to **`409 Conflict`** with a human-readable detail message (`"… cannot resume — the queue is empty or the zone is mid-transition. Pick something to play from the Sonos app first."`); the front-end surfaces that detail through the action-error toast (see below) and then runs the standard `clearPendingPrediction → refresh()` recovery so the tile snaps to reality. `_bulk_pause_sonos_apply_impl` catches the same domain error and silently drops the offending zone from both `affected` and `skipped` (it isn't excluded — the user didn't ask — and it wasn't paused either), so one stuck zone never takes down a global "Turn off / pause / close everything" action. Other UPnP fault codes propagate as-is — only 701 has a known graceful translation.
- **Family header icons.** Each family-section renders an inline SVG icon next to its `<h3>` title — lightbulb for `kasa` ("Lights & plugs"), speaker for `sonos` ("Sonos zones"), pitched-roof house for `tailwind` ("Garage doors"). Icons live in `FAMILY_ICON_PATHS` in `web/src/main.ts` as plain SVG `d=` strings; `createFamilyIcon(family_id)` builds the `<svg>` in the SVG namespace (`createElementNS` — `createElement` would produce inert HTMLUnknownElement nodes), wraps the paths in `stroke="currentColor"` so the CSS rule `.family-icon { color: var(--family-color, var(--muted)); }` paints them per-family, and stamps `aria-hidden="true"` because the adjacent `<h3>` text is the real label for assistive tech. Adding a new family is a one-line entry here plus the existing `color` field in `app.api.ui_state._FAMILIES`. The icons replaced an earlier 10×10 `::before` colored square — the family colour cue stayed (now carried by the SVG fill instead of a swatch), and the icon adds a glanceable category hint.
- **Action-error toast for recoverable failures.** `DomestiBotController.renderActionError(message)` mounts a dismissible toast on `document.body` (so the background poll's `render()` — which calls `replaceChildren()` on `#app` — never blows it away). One toast at a time: a subsequent error replaces the current one and resets the 10-second auto-dismiss timer; the user can also click the `×` button. Wired from `onToggleSonos`'s catch when `err instanceof HttpError && err.status === 409` — currently the only path with a user-actionable server-side hint. `HttpError.detail` (parsed from FastAPI's `{"detail": "…"}` body at construction time, memoised) carries the message. Distinct from `renderError`, which is **destructive** (replaces all of `#app`) and reserved for fatal/bootstrap failures. Reuse `renderActionError` for any future endpoint that returns 4xx with a hint the user can act on; non-actionable errors should keep falling back to `console.warn` + `refresh()`.
- **Google Cast bring-up is temporarily disabled.** `ANDROIDTV_TEMPORARILY_DISABLED = True` in `app/androidtv_device_manager.py` short-circuits `boot_androidtv()` in `app.domesti_bot_cli.bootstrap_device_managers`, so no Cast tile appears in the web UI, the REPL `androidtv` family stays empty, and the startup banner reports `Google Cast: skipped — temporarily disabled — TODO(google-cast-on-off): …`. The manager class, discovery helpers, SQLite cache schema (`androidtv_discovered_hosts`), and the entire `tests/python/test_androidtv_device_manager.py` suite stay in place so the eventual fix lands without recreating scaffolding. **Root cause to investigate** (`TODO(google-cast-on-off)`): the Cast control path turns devices *on* but does not reliably turn them *off* in the field — likely a session / quit_app interaction with the cached host tuple. When fixed, delete both the constant and the short-circuit branch (grep for `google-cast-on-off`); the `--no-androidtv` CLI flag then resumes its original meaning.
- **Kasa KLAP credentials (newer Tapo / cloud-linked plugs).** TP-Link's newer KLAP protocol has **no anonymous LAN mode**: a device that was paired through the Kasa or Tapo phone app requires the *account email + password* for the LAN handshake. Without them, `Discover.discover` finds the device but `dev.update()` (and every recovery path) raises `AuthenticationError`. Resolution order (`app.kasa_credentials.resolve_kasa_credentials`): `KASA_USERNAME` + `KASA_PASSWORD` env (both, or neither — partial is rejected) → Fernet-encrypted `app_secrets` rows (`kasa_username` / `kasa_password`). Credentials are applied **per host**: UDP discovery stays anonymous; only hosts with `requires_klap_auth` in `kasa_discovered_devices` (exposed as `hosts_requiring_klap_auth`) receive account credentials on reconnect/update — legacy XOR / anonymous-KLAP devices never get credentials attached. Surfaces:
  - **Settings UI** — ☰ menu → Settings → **Kasa** (`GET`/`PUT`/`DELETE /v1/settings/kasa-credentials`). Username and password are returned on `GET` when stored in the database (Settings is API-key protected). Status lists `hosts_requiring_klap_auth` and `skipped_auth_hosts`. Save hot-reloads credentials into the running `KasaDeviceManager` and runs `rediscover()` (plus state-watcher restart).
  - **Persistent env** — still supported and overrides the database; the systemd unit may load them via `EnvironmentFile=` so they never appear in `ps aux` or shell history. The user-facing WARNING gets an actionable suffix from `_klap_auth_recovery_hint`.
  - **REPL `kasa-creds`** — prompts for email (visible) and password (`is_password=True`); calls `set_credentials` + `rediscover()`, and when a Fernet key is configured writes encrypted `app_secrets` so credentials survive restart. `_repl_cmd_kasa_creds` takes `prompt_fn` as a dependency so tests can exercise it without prompt_toolkit's terminal layer.
- **GoTailwind token (encrypted SQLite).** The six-digit Local Control Key can be stored in `app_secrets` (Fernet ciphertext) instead of plain environment variables. Resolution order for the token: CLI `--tailwind-token` → `TAILWIND_TOKEN` env → decrypted row in SQLite (`app.tailwind_credentials.resolve_tailwind_token`). The Fernet master key loads from `DOMESTI_BOT_SECRETS_KEY` or from gitignored `domesti-bot.config.json` at the repo root (`domesti_secrets_key` field — see `domesti-bot.config.json.example`). Override the file path with `DOMESTI_BOT_CONFIG_FILE`. **`setup-secrets` REPL command** — interactive helper in `app.domesti_bot_cli` that generates or accepts a Fernet key and writes `domesti-bot.config.json` (mode `0600`). **Desktop web UI** — ☰ menu → Settings (`PUT /v1/settings/tailwind-token`); token is never returned on read (`GET /v1/settings/tailwind-token` reports status only). After saving, restart the server so discovery reloads the token.
- **Settings credential Test (read-only).** Each auth card (Tailwind, Kasa, My Tracks, Vizio) has a **Test** button that calls `POST /v1/settings/…/test` (Vizio: `…/tvs/{device_id}/auth/test`). Probes use ephemeral clients only (`app.settings_credentials_test`) — they never call `rediscover` / `disconnect` on live managers. Credential failures return **200** with `ok: false` and a `detail` string; **4xx** only when the test cannot run (no credentials, no KLAP hosts, missing My Tracks password/domain, unknown Vizio `device_id`). Optional form overrides (token / username+password / host) win over stored CLI/env/database resolution.
- **Compact tile icons (static SVG).** Per-device compact tiles load stroke-style SVG files from `app/api/static/icons/compact/<key>.svg` (keys from `UIDeviceOut.compact_icon` / `app.ui_compact_icon.resolve_compact_icon`). Each file is a committed **24×24** asset with `stroke="currentColor"` so tile tinting works via CSS. The web UI fetches and inlines each file at runtime (`web/src/main.ts` — `COMPACT_ICON_BASE`, `loadCompactTileIconInto`). Garage doors use `garage_open` / `garage_closed` based on live state. Whenever you add or change any file under `icons/compact/`, run `./scripts/internal/generate-compact-icon-preview` and **attach** the generated `$HOME/scratch/domesti-bot/tmp/build/review.html` (or a screenshot) to the **PR description** — the gallery is not committed or served from the repo tree (override output with `DOMESTI_COMPACT_ICON_REVIEW_DIR` or `--output-dir`).
- **Static assets.** `app/api/static/` is mounted at `/static/` via `StaticFiles`. Source files (HTML, PWA manifest, service worker, icons) live there directly and are committed; the `dist/` subdirectory is gitignored and rebuilt by `pnpm run build` (see "Web UI" below). The mount is unconditional — a missing `dist/main.js` 404s cleanly without breaking `/`.
- **PWA.** `index.html` links `manifest.webmanifest`; `web/src/main.ts` registers `/sw.js` (served at the URL root, not only under `/static/`, so the worker scope covers the whole app). Install prompts require HTTPS or loopback; LAN-only HTTP still gets manifest metadata in supporting browsers.
- **Favicon.** `GET /favicon.ico` returns `204 No Content` so browser auto-fetches don't generate 404 noise. Do not ship a real icon binary; if branding is needed in the future, prefer an inline SVG behind a separate route.

---

## Web UI

The browser-side dashboard lives in `web/` and is built with **pnpm + esbuild + typescript**. Compiled output is written to `app/api/static/dist/` and served by the Python FastAPI app at `/static/dist/`. The Python server has **no Node dependency at runtime** — Node is required at build time only (locally, in CI, and by `scripts/on-deploy` in production).

**Toolchain pins** (in `web/package.json`):

- `packageManager: "pnpm@10.33.4"` — corepack reads this and installs the exact pnpm version on demand. Do not bump pnpm to v11+ without re-validating `onlyBuiltDependencies` behavior (v11.0/11.1 silently ignored that field for esbuild's postinstall).
- `engines.node: ">=20"` matches the CI matrix and `web/README.md`.
- `pnpm.onlyBuiltDependencies: ["esbuild"]` — pnpm v10+ blocks postinstall scripts by default; esbuild needs its native binary downloaded, so it is explicitly allowlisted. Adding a new dep with a postinstall (sharp, better-sqlite3, etc.) requires extending this list **and** justifying it.

**Layout rules**:

- TypeScript sources go under `web/src/`. No browser code lives in `app/api/static/` — that directory is for committed static assets (HTML, future CSS) and the gitignored build output (`dist/`).
- One `esbuild` call (`web/build.mjs`). No Vite, Webpack, Rollup, or framework-specific dev servers. If a future feature genuinely needs more, that is a documented escalation in this section.
- `tsconfig.json` is **strict** (`strict`, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`, `verbatimModuleSyntax`, `isolatedModules`). New code does not opt out without a comment explaining why.
- No frameworks (React, Vue, Svelte, …) in PR1. The first concrete proposal to add one must call this section out and update it as part of the same PR.

**Day-to-day commands** (run inside `web/`):

```
pnpm install --frozen-lockfile   # bootstrap deps from pnpm-lock.yaml
pnpm run typecheck               # tsc --noEmit
pnpm run build                   # esbuild → ../app/api/static/dist/main.js
pnpm run watch                   # rebuild on change (dev only)
pnpm run check                   # typecheck + build (mirrors the CI job)
```

**CI job** (`.github/workflows/ci.yml#web-build`): installs Node ≥ 20, enables corepack, runs `pnpm install --frozen-lockfile`, then `pnpm run typecheck`, `pnpm run build`, and asserts `app/api/static/dist/main.js` exists. A bundle that builds with no output files (silent esbuild misconfiguration) fails CI on the assertion step rather than mysteriously serving 404 in production.

**Deploy** (`scripts/on-deploy`): the hook checks for `node` + `corepack` on PATH (deploy aborts with exit 2 if either is missing), runs `corepack enable pnpm`, then `pnpm install --frozen-lockfile` and `pnpm run build` inside `web/`. The Python smoke-import (`uv run python -c "import config.serve"`) runs after the bundle build, so a missing bundle does *not* fail the hook (the FastAPI app starts fine without it; users just see a `pending` status pill in the landing page).

**The `dist/` contract**:

- Filename is stable (`main.js` / `main.js.map`); no content hashing in PR1. When tile assets need cache-busting we will add hashed names + a manifest, not before.
- `app/api/static/index.html` references `/static/dist/main.js` literally. Both filenames are part of the contract — changing one without the other breaks the page.

---

## Shell Scripts

- **`scripts/internal/`** — maintainer-only helpers (icon preview regeneration, one-off imports). Not invoked by CI, deploy, or the running server. Prefer a single focused script per workflow instead of growing top-level `scripts/`.
- **No file extension on executables.** Runnable scripts under `scripts/` (bash **or** Python) are extensionless kebab-case names (`scripts/domesti-bot`, `scripts/on-deploy`, `scripts/internal/generate-compact-icon-preview`). The shebang declares the interpreter. Do **not** add `.sh` or `.py` to entrypoints users execute directly — reserve `.py` for modules invoked only via `uv run python path/to/module.py` (e.g. `scripts/embed_build_metadata.py`, `scripts/verify_google_cast_discovery.py`).
- **No `.sh` extension.** (Same rule as above; called out because `.sh` is a common mistake for bash.)
- Use `#!/usr/bin/env bash` and `set -euo pipefail` at the top of every script.
- **`shellcheck`** is mandatory for all **bash** scripts (files with `#!/usr/bin/env bash`). Run `shellcheck <script>` before committing; resolve every finding (or annotate the line with `# shellcheck disable=SCxxxx` plus a comment explaining why). CI skips extensionless Python entrypoints under `scripts/` automatically (python shebang); do not rename them to `.py` to satisfy shellcheck.
- **Non-exported variables are lowercase.** Uppercase is reserved for exported environment variables.
- **`readonly`** for any script-level variable assigned once. Declare and assign separately to avoid masking exit codes (SC2155):
  ```bash
  var="$(some_command)"
  readonly var
  ```
- **`local` for all function-scoped variables.** Use `local -r` only for literal parameter assignments — never `local -r foo=$(cmd)` (SC2155).
- **No Python in infrastructure scripts.** Bootstrap scripts (e.g. `scripts/on-deploy`) must not invoke `python3` for utility operations (URL parsing, secret generation, encoding). Use pure bash + `openssl` / `tr` / `printf` — Python may be absent or in a broken venv at deploy time. The exception is a post-`uv sync` smoke import (`uv run python -c "import config.serve"`) at the end of the deploy hook, which is exercising the freshly-installed environment by design.

---

## Standalone Python CLI Scripts

- Executable Python CLIs use **no `.py` extension** — kebab-case, extensionless paths like `scripts/domesti-bot` or `scripts/internal/generate-compact-icon-preview` (same rule as bash entrypoints above).
- `chmod +x` them and use shebang `#!/usr/bin/env python3`.
- They must **auto-activate the uv environment** so they work without manual venv activation:
  ```python
  import os
  import shutil
  import sys

  def _ensure_uv() -> None:
      """Re-exec under `uv run` if not already in the managed environment."""
      if os.environ.get("UV_ACTIVE") or "/.venv/" in (sys.executable or ""):
          return
      uv = shutil.which("uv")
      if uv:
          os.execv(uv, [uv, "run", sys.argv[0], *sys.argv[1:]])

  _ensure_uv()
  ```
- For new CLI tools prefer **Typer** for argument parsing. Existing modules that use `argparse` (e.g. `app/domesti_bot_cli.py`, `config/serve.py`) may continue to use it for consistency with their current style.

---

## Logging

The strategy is intentionally stable so operator tail / grep recipes keep working across upgrades.

**Library code**

- Use the stdlib `logging` module — module-level `_LOGGER = logging.getLogger(__name__)`. Never `print()` from library code.
- Prefer **one log record per event** with a single formatted message. Avoid adjacent `logger.*` calls for the same event.
- For exceptions, prefer `logger.exception("...")` or `logger.error("...", exc_info=True)` — do not pair an `error()` with a separate `exception()` for the same failure.
- When the formatted message is expensive and the level may be disabled, guard it: `if _LOGGER.isEnabledFor(logging.DEBUG): ...`.
- Per-request HTTP access logs are emitted by the FastAPI middleware in `app/api/app.py` (tag `[http]`). Do not log the same request a second time inside handlers.
- Use **transport tags** at the start of the message for client-activity records: `[http]`, future `[http-tls]`, `[ws]`. Reserve plain (untagged) lines for internal lifecycle events (startup, shutdown, discovery results).

**Configuration**

- The dict-config and the `LocalTimeFormatter` live in `app/logging_config.py`. The launcher exports env vars; the Python process calls `apply_logging_from_env()` before uvicorn boots.
- Format: `YYYYMMDD-HH:MM:SS.mmm | LEVEL    | module       | message`.
- Timestamps render in the **system timezone** by default. Set `LOG_UTC=1` (or pass `--log-utc`) to switch to UTC.
- Custom **`TRACE`** level (numeric value 5, below `DEBUG`) is auto-registered for high-volume per-request lines. The `HealthCheckFilter` demotes `/health` access lines to `TRACE` so they never pollute `INFO` output. A complementary path-based demotion lives in `app/api/app.py`: paths in `_QUIET_ACCESS_LOG_PATHS` (currently just `/v1/ui/state`, the web UI's 5-second poll) emit their **successful** `[http]` lines at `DEBUG` so they're invisible at the default `INFO` level but resurface when you turn the dial up to debug a real issue. Failure responses (`>= 400`) for the same paths still log at `INFO` so genuine errors stay visible. Add new noisy poll endpoints to `_QUIET_ACCESS_LOG_PATHS` rather than introducing new filters or per-handler log suppression.
- **File logging**: when `LOG_FILE` is set (default `$HOME/scratch/domesti-bot/domesti-bot.log`, under a per-user `$HOME/scratch/` tree for easy discovery), a `RotatingFileHandler` writes 10 MB files with 5 backups. `--no-log-file` disables file output entirely.
- **Dual logging**: pass `--console` to keep the file destination *and* mirror to stdout — useful during development.
- **Levels** are controlled by `--log-level {trace,debug,info,warning,error,critical}` (default `info`). The flag sets `DOMESTI_LOG_LEVEL`, which is applied to the root logger, the `app.*` namespace, and all uvicorn loggers (`uvicorn`, `uvicorn.error`, `uvicorn.access`).

**Launcher flags** (`scripts/domesti-bot-server`):

| Flag | Env var | Notes |
| --- | --- | --- |
| `--log-level LEVEL` | `DOMESTI_LOG_LEVEL` | Default `info`. Accepts `trace`. |
| `--log-file PATH` | `LOG_FILE` | Default `$HOME/scratch/domesti-bot/domesti-bot.log`. |
| `--no-log-file` | (unsets `LOG_FILE`) | Console-only mode. |
| `--console` | `DOMESTI_LOG_CONSOLE=1` | Force-enable console even when a file is in use. |
| `--log-utc` | `LOG_UTC=1` | Print UTC instead of local time. |

Everything else is forwarded to `python -m config.serve` (after `--`, or simply intermixed — unknown flags pass through).

---

## Security

- **Never log, store, or transmit credentials** (`KASA_PASSWORD`, `TAILWIND_TOKEN`, `DOMESTI_API_KEY`, `domesti_secrets_key`) in plain text. Read them from the environment or from mode-`600` files; do not echo them to stdout or commit `.env` / `domesti-bot.config.json` (the latter is gitignored; ship only `domesti-bot.config.json.example`).
- **Wildcard HTTP (`0.0.0.0`)** is only appropriate on a trusted home LAN or behind proper auth/TLS. The `etc/systemd` user unit intentionally listens on all interfaces at port 8003 (no colocated nginx)—set `DOMESTI_API_KEY` (or front the service) before exposing that pattern anywhere less trusted.
- **Validate user-controlled paths** (REPL filenames, future upload endpoints) with `pathlib.Path.resolve()` before any filesystem operation; reject paths that escape the working directory.
- **No `eval`, `exec`, or `subprocess.run(..., shell=True)`** with user-controlled strings.
- **Passwords / tokens never appear in shell command arguments** — they end up in `~/.bash_history` and in `ps aux`. Pass them via stdin, environment variables loaded from root-readable files, or systemd `EnvironmentFile=`.
- **Persisting `TAILWIND_TOKEN` (and similar) for a systemd service** — pick one:
  - **`EnvironmentFile=`** on the unit (or a **drop-in** via `systemctl edit`): path to a mode-`600` file owned by root or the service user, one `KEY=value` per line. Keeps secrets out of `ExecStart=` and the main unit file; rotate by replacing the file and restarting.
  - **User units**: `~/.config/environment.d/*.conf` exports variables for `systemctl --user` sessions after `daemon-reload` / re-login (see systemd.environment-generator(7)); still keep the file private to that user.
  - **`LoadCredential=` / credential pick-up** (systemd 247+): store the token in a root-only file and pass it via the credentials protocol if the process is taught to read `$CREDENTIALS_DIRECTORY` — the stock launcher today reads **environment only**, so this path needs small launcher glue before it is turnkey.
  - **Avoid** plain `Environment=TAILWIND_TOKEN=…` in a shared or world-readable unit: it is easy to leak via `systemctl cat` copies and process listings.

---

## Repository

- Remote: `https://github.com/the-hcma/domesti-bot.git` (private).
- Do not make the repository public without explicit user approval.
- Never commit secrets, credentials, or API keys — use environment variables / `EnvironmentFile=` for systemd.

---

## Commits, Stacking & Pull Requests

> See **`docs/GRAPHITE.md`** for stacked PRs, merge queue (`merge-it` label), and GitHub ruleset bypass for `graphite-app`. Conventions below match sibling repos in the org.

- This project uses **Graphite (`gt`)** for branch stacking. All work happens in stacked branches.
- **Never commit or push directly to `main`.** `main` is updated only via merged PRs. Enforcement layers, in order of strength:
  - **Client-side pre-push hook** (`scripts/hooks/pre-push`, wired by running `./scripts/install-hooks` once per clone). Aborts any `git push` whose remote ref is `refs/heads/main` with a tutorial message pointing at `gt` / `gh pr create`. Bypass for the rare mirror/rescue case: `git push --no-verify origin main`.
  - **Cursor rule** (`.cursor/rules/pr-workflow.mdc`, `alwaysApply: true`) tells the agent to refuse any "commit to main" intent and to open a PR instead. Applies to every agent session.
  - **Server-side branch protection** is the strongest layer but requires GitHub Pro on private repos (or making this repo public). Until either is in place we rely on the two layers above; once enabled, swap in the ruleset documented in `.cursor/rules/pr-workflow.mdc`.
- **Worktree-per-stack.** Every new stack/PR is created in its own Git worktree via `~/work/ai/repository-helpers/scripts/dev/start-development` so concurrent stacks stay isolated.
- **Branch / commit creation**: `gt create --all --message "feat: descriptive message"`. Always use full flags (`--all`, `--message`), never the combined `-am`.
- **Amending an existing PR** (corrections, review fixes, fixups): `gt modify --no-edit` (staged changes only) or `gt modify --all --message "updated msg"`. Do not create new commits on the same branch for these — fold them in.
- **Squashing fixups before submit**: `git reset --soft HEAD~<n>` to collapse, then `gt modify --no-edit` to fold into the top commit.
- **Submitting**: `gt submit --no-interactive --publish` — pushes the branch and creates a ready-for-review (non-draft) PR. `--publish` belongs on `gt submit`, never on `gt create`.
- **Filling in PR description** after submit (Graphite doesn't take a body flag):
  ```
  gh api repos/the-hcma/domesti-bot/pulls/<pr> --method PATCH --field body="..."
  ```
- **Sync**: `gt sync --force` after upstream PRs land.
- **View stack health**: `gt log short` — verify parent order, no "needs restack", no diverged branches.
- **Pruning**: periodically `gt fetch --prune && git branch -vv | grep ': gone]' | awk '{print $1}' | xargs -r git branch -D`.
- **Commit messages** follow Conventional Commits: `feat:`, `fix:`, `chore:`, `docs:`, `test:`, `refactor:`, `perf:`.
- **GPG-signed commits.** `commit.gpgsign = true` in git config; signing key uploaded to GitHub so commits show as "Verified".
- **No AI attribution.** Do not add `Co-Authored-By: Claude`, `Generated-By:`, or similar to commit messages or PR descriptions.

### Pull request workflow

1. **Pre-PR quality gates** — all must pass locally before submit (these mirror the CI jobs in `.github/workflows/ci.yml`). Use `uv sync --group dev` first:
   ```
   uv sync --group dev
   uv run pyright                                    # type errors over app/, config/, scripts/, tests/
   uv run pytest -m "not integration and not browser" -n auto   # hermetic (CI parallel job)
   uv run playwright install chromium                # once per machine; Linux may need --with-deps
   uv run pytest -m "browser"                        # layout probes (CI browser job)
   shellcheck $(git ls-files scripts | grep -Ev '\.(py|md|txt|yml|yaml|json|toml)$')
   uv run --with pip-audit pip-audit                 # CVE check on runtime deps (daily in CI)
   ```
2. **Submit**: `gt submit --no-interactive --publish`.
3. **Verify stack on GitHub**:
   ```
   gh pr view <pr> --json number,title,baseRefName,mergeable,mergeStateStatus,files
   ```
   `mergeable` must be `MERGEABLE`; `mergeStateStatus` must be `CLEAN` or `BLOCKED` (never `DIRTY` / `CONFLICTING`).
4. **Verify title and description** against the actual diff — titles written before a rebase/restack go stale fast. Update via `gh api ... --method PATCH --field title=... --field body=...`.
5. **Wait for CI** to pass. Do not ask the user to test before CI is green. Poll with short waits: `sleep 10 && gh pr checks <pr>`.
6. **User testing & approval** — explicit user approval is required before merge.
7. **Merge**: add the `merge-it` label via `gh pr edit <pr> --add-label merge-it` to enqueue on the **Graphite merge queue** (see `docs/GRAPHITE.md`). **Never** run `gh pr merge` directly. **Always ask the user for explicit confirmation before adding the merge label** — it starts automated queue processing.

### Single Responsibility per PR

- Each PR addresses **one concern** (one feature, one bug fix, one refactor). Do not mix unrelated changes.
- The PR title and description must accurately describe that single concern. If the work has grown to multiple concerns, split into separate stacked PRs.
- PR descriptions follow the **Summary + Test plan** format at minimum.

---

## Server Management (development)

- **User installs** (typical dev / home server): `~/work/ai/repository-helpers/scripts/setup-service` reads `etc/systemd/domesti-bot.service`, substitutes `@@REPO_DIR@@`, installs `~/.config/systemd/user/domesti-bot.service`, and runs `scripts/on-deploy` before (re)start — same pattern as fpdf's `etc/systemd/fpdf.service`.
- **System installs** (multi-user.target, dedicated service account): use `production/systemd/domesti-bot-server.service.template` with `@@REPO_ROOT@@` / `@@SERVICE_USER@@` and install under `/etc/systemd/system/` yourself; that path is **not** consumed by `setup-service`.
- **User unit listen** is **`0.0.0.0:8003`** via `--listen-all --listen-port 8003` in `ExecStart` (fpdf-style LAN reachability). After start, **`ExecStartPost`** runs `curl` against `http://127.0.0.1:8003/health` (the public `GET /health` route) with retries so systemd only reports *active* once HTTP answers—even though the bind address is all interfaces, loopback to the same port still works. If you change the port in the unit, update both `ExecStart` and `ExecStartPost` so they stay in sync. The **system** template in `production/systemd/` still documents `127.0.0.1:8003` for nginx-colocated installs.
- **During development / testing**: do not start the production server manually. The session-init script (`start-development --refresh`) ensures the background service is running.
- Manual runs for debugging: `./scripts/domesti-bot-server` (forwards all flags to `python -m config.serve`).
- **Do not curl/HTTP against the running production port (8003) during automated testing.** Tests must exercise the ASGI app directly via `httpx.AsyncClient(app=app)` so they cannot collide with the live server.

### Blank browser / empty `#app` on a real host

Typical causes (check in order):

1. **Wrong URL or bind address** — If the unit still used loopback-only (`127.0.0.1`) and you open `http://<this-host's-LAN-IP>:8003/` from another machine, the connection never reaches the process. The `etc/systemd` unit uses `--listen-all` so `0.0.0.0:8003` accepts LAN clients; confirm with `ss -ltnp | grep 8003` (or `lsof -iTCP:8003 -sTCP:LISTEN`). Firewall rules must allow TCP **8003** on the interfaces you use.
2. **Missing web bundle** — `GET /` returns HTML immediately, but the tile UI lives in `app/api/static/dist/main.js` (gitignored). If that file was never built, the browser shows an empty shell until the static boot hint (or nothing, on older HTML). Run `setup-service` so `scripts/on-deploy` runs `pnpm run build` in `web/`, or build manually once: `cd web && pnpm install --frozen-lockfile && pnpm run build`. Confirm `GET /static/dist/main.js` returns **200** (not **404**).
3. **`on-deploy` exit 1 (no restart)** — The hook exits **1** only when `HEAD`, the deploy-input fingerprint, `.venv`, `web/node_modules`, `app/api/static/dist/main.js`, and the installed systemd unit's checkout path all match the last successful deploy for this worktree. It rebuilds automatically when the bundle is missing, the Python venv or pnpm tree is out of sync, deploy inputs changed (content hash), the cache is legacy or shared across checkouts, or the unit still points at a different checkout (run `setup-service` from the tree you want).
4. **API key** — With `DOMESTI_API_KEY` set, unauthenticated browser calls to protected JSON routes return **401**; the shell still loads if the bundle exists. Open `GET /health` (no key) to verify HTTP, then configure the key in the client or relax auth for debugging only.

### Discovery Cache (cache-first startup)

Device discovery is **cache-first**: the LAN probe runs only when the SQLite discovery cache (`$HOME/.cache/rule-engine/device_discovery.sqlite` by default; override with `--discovery-cache`) is empty for that backend or the cached state fails to reconnect. Pass `--force-discovery` to bypass the cache for all backends.

The cache schema lives in `app/device_discovery_store.py` (facade) and `app/db/` (SQLAlchemy ORM + `bootstrap_schema`). One SQLite file, one table per backend. All schema changes are **additive only** via `CREATE TABLE IF NOT EXISTS` — existing on-disk caches from pre-SQLAlchemy releases keep working; legacy `ALTER TABLE` steps still run for older Cast rows missing `uuid` / `friendly_name`. The `app_secrets` table is new (encrypted Tailwind token and future secrets).

Per-backend behavior:

| Backend | Cache table | Cache hit ⇒ |
| --- | --- | --- |
| **Kasa** | `kasa_discovered_devices` (host, alias, config_json) | Reconnect each cached host with the saved `DeviceConfig`. Falls back to full UDP discovery if **any** host fails to reconnect. |
| **Cast (AndroidTV)** | `androidtv_discovered_hosts` (host, port, friendly_name, **uuid**, **model_name**) | **No-mDNS fast path** when every row has a non-empty `uuid`: build a host tuple per row and call `pychromecast.get_chromecast_from_host` directly, in parallel, with a short timeout (`_CACHE_FAST_CONNECT_TIMEOUT_S = 2 s`). A dead cached device is dropped with a warning — **no fallback to mDNS** (use `--force-discovery` for that). If any cached row pre-dates the uuid migration (uuid IS NULL), the manager falls back to a targeted/full mDNS browse that rewrites the cache with the new metadata so the *next* startup gets the fast path. |
| **Sonos** | `sonos_known_zones` (uuid, host, zone_name) | Construct `soco.SoCo(host)` per row and verify `.uid` matches the cached UUID. Mismatch / unreachable host on **any** row falls back to UDP SSDP discovery, then rewrites the cache. |
| **Tailwind** | `tailwind_last_host` (singleton) | Try the cached host first; on failure, run mDNS discovery and update the cache. |

All managers expose a stable identifier (Kasa: alias or host, Cast: UUID, Sonos: `RINCON_*` UID, Tailwind: hostname). Display-name overrides live in `device_display_names`, keyed by `(backend, canonical_key)`.

**`last_discovery_source` reporting**. Each LAN-discovering manager (`KasaDeviceManager`, `SonosDeviceManager`, `AndroidTvDeviceManager`) MUST set `self._last_discovery_source` to `"cache"` or `"discovery"` at the end of `fetch()` and expose it via a `last_discovery_source: str | None` property. The semantics are precise — they directly drive the per-backend "ready" line that the user reads to decide if a slow start is suspicious:

- `"cache"` — **no broadcast / multicast / SSDP / mDNS traffic** during this fetch. The manager reconnected to every cached endpoint directly (e.g. `SoCo(host).uid` for Sonos, `Discover` with a saved `DeviceConfig` for Kasa, `pychromecast.get_chromecast_from_host` for Cast). A dead cached device is dropped with a warning; we do not fall back to LAN discovery (use `--force-discovery` for that).
- `"discovery"` — **any LAN sweep ran**, including targeted-mDNS modes that pre-filter by cached hosts. From the user's perspective, "discovery" means traffic hit the network and the wall-clock includes a multicast probe.

The CLI bootstrap renderer (`_print_family_parallel_line` in `app/domesti_bot_cli.py`) reads this signal — together with the device count — and annotates each backend's "ready" line, e.g.:

```
Discovering devices (parallel)…
  Google Cast: ready (cache, 5 devices)
  GoTailwind: skipped — no token — set TAILWIND_TOKEN or --tailwind-token
  Kasa: ready (LAN discovery, 9 switches)
  Sonos: ready (cache, 3 zones)
```

This is the canonical user-facing answer to "is this a fresh sweep or a cache hit?" — keep it accurate per-backend. Tailwind has no LAN broadcast (it uses an HTTP API), so its bundle leaves `source` as `None` and the renderer simply omits the source annotation (`GoTailwind: ready (2 doors)`). The renderer is also tolerant of older bundles missing `source`/`count` fields and falls back to bare `ready`; tests in `tests/python/test_domesti_bot_cli.py` lock both shapes.

When adding a new backend, follow the same pattern: a dedicated table, a `load_<backend>()` and `save_<backend>()` pair in `device_discovery_store.py`, a `discovery_cache_path` + `force_discovery` pair on the manager constructor, a cache-first branch at the top of `fetch()`, **and** a `last_discovery_source` signal set in both branches of `fetch()` so the bootstrap line is accurate.

### Deploy hook (`scripts/on-deploy`)

`setup-service` from `repository-helpers` calls `scripts/on-deploy` before starting or restarting the systemd unit. Implement the exit-code contract documented below so the shared installer can drive restarts without per-repo customization.

**Exit-code contract** (the hook MUST NOT restart the unit itself — that responsibility belongs to `setup-service`):

| Exit | Meaning | `setup-service` action |
| --- | --- | --- |
| `0`  | Steps executed; service must be restarted to pick up changes. | Restart the unit. |
| `1`  | Nothing changed since the last successful deploy. | Skip the restart. |
| `≥2` | Unexpected failure. | Abort the deploy. |

**What the hook does** (in order):

1. `set -euo pipefail` and a `trap … ERR` that prints the failing line + command + exit code so failures are diagnosable.
2. Add `$HOME/.local/bin` to `PATH` so `uv` is discoverable from `setup-service`'s non-login shell.
3. Verify `uv` is on `PATH`; exit `2` if missing.
4. Ensure `.venv/` exists with a usable interpreter — recreate it via `uv sync` if missing or stale (handles brand-new worktrees).
5. Reset a stuck `domesti-bot.service` failed state for the **user** manager (`systemctl --user`) when not root; when root, reset the system manager (best-effort; ignored if the unit isn't installed yet).
6. Skip when `HEAD`, a hash of deploy inputs (`pyproject.toml`, `uv.lock`, `app/`, `config/`, `web/`, `app/api/static/icons/`, `index.html`, `sw.js`, …), `.venv`, `app/api/static/dist/main.js`, and the user unit's `ExecStart` checkout path all match the **per-repo** state file (`$HOME/scratch/domesti-bot/on-deploy-sha-<hash>`, derived from the checkout path so linked worktrees do not share one cache; override with `ON_DEPLOY_SHA_FILE`). Rebuild when the bundle or venv is missing/out of sync, the input hash changed, the cache is legacy or shared (`on-deploy-sha` without a path suffix), or systemd still serves another checkout. Skip → exit `1`.
7. `uv sync --frozen` — refuse to mutate `uv.lock` on a deploy box; build either matches the committed pin or fails loudly.
8. `pnpm install` + `pnpm run build` in `web/` when present.
9. Smoke-import `config.serve` so a broken dep or syntax error fails the hook BEFORE `setup-service` restarts the unit.
10. Record deploy state (`commit` + `inputs` hash), exit `0`.

**Manual runs**: `./scripts/on-deploy` (idempotent; auto-detects when a rebuild/restart is needed). `--help` prints the header docblock.

---

## Data Safety

- **Never delete, modify, or purge user data without explicit user approval.** This includes the SQLite discovery cache (`device_discovery_store`), Kasa credentials in keystores, and any rule-engine persisted state.
- **Always ask before** running destructive operations (cache wipes, schema migrations that drop columns, etc.).
- If a fix requires deletion, present the impact first ("This will drop 14 cached AndroidTV hosts. Proceed?").

---

## CI Checks (all must pass)

CI lives in `.github/workflows/`:

- **`ci.yml`** — runs on every PR (skipping merge-queue staging branches and already-merged PRs). Jobs after `Guard` run **in parallel**:
  - `Pyright` — `uv sync --group dev`, then `uv run pyright`
  - `Pytest (hermetic)` — `uv sync --group dev`, then `uv run pytest -m "not integration and not browser" -n auto` (**pytest-xdist**)
  - `Pytest (browser layout)` — `uv sync --group dev`, `playwright install --with-deps chromium`, then `uv run pytest -m "browser and not integration"` (single process; parallel to hermetic job)
  - `Shellcheck` — every no-extension script under `scripts/`
  - `Web (typecheck + build)` — `pnpm install --frozen-lockfile`, `pnpm run typecheck`, `pnpm run build`, asserts `app/api/static/dist/main.js` exists
  - `Workflow Lint (actionlint)` — validates the YAML in `.github/workflows/`
  - `Secret Scan` — `gitleaks` on the PR diff (full repo on schedule)
- **`cve-check.yml`** — `pip-audit --strict` daily at 08:00 UTC against the synced uv environment.
- **`cleanup-branch-on-merge.yml`** — deletes the head branch when a PR is merged.
- **`cleanup-merged-branches.yml`** — daily sweep for stragglers (merged or closed >30 days).
- **`merged-pr-closer.yml`** — closes open PRs whose changes have already landed on `main` (handles Graphite merge-queue cases where child PRs are left open).
- **`dependabot-auto-merge.yml`** — auto-labels Dependabot PRs with `merge-it`.

Dependabot itself is configured in **`.github/dependabot.yml`**: weekly Monday sweeps across `pip` (root `pyproject.toml`), `npm` (`/web`), and `github-actions` (`/`), all labeled `dependencies` so the auto-merge workflow picks them up. Patch + minor bumps are grouped into a handful of named buckets (`fastapi-stack`, `pytest-stack`, `typescript`, `esbuild`) to keep the PR count down; major bumps continue to land as individual PRs for review. Version-update PRs use a **10-day cooldown** (`cooldown: default-days: 10`); **security updates are not cooldown-gated**.

### Dependency release age (dep-updater 9 days, Dependabot 10 days)

Aligned with [repository-helpers](https://github.com/the-hcma/repository-helpers) `AGENTS.md` — **dep-updater** lands updates before Dependabot:

| Layer | Mechanism |
|-------|-----------|
| **pnpm** (`web/`) | `minimumReleaseAge: 12960` (9 days) in `web/pnpm-workspace.yaml`. `minimumReleaseAgeExclude: ["*"]` grandfathers the **existing lockfile at cutover** so CI keeps working. |
| **dep-updater** | 9-day gate for npm, Python/PyPI, and GitHub Actions bumps. |
| **Dependabot** | Weekly scan + `cooldown: default-days: 10` on **version-update** PRs (pip, npm, github-actions; one day after dep-updater). Do **not** set `open-pull-requests-limit: 0` — version updates stay enabled as a backup. |
| **CI** | Daily `cve-check.yml` (`pip-audit --strict`) on the uv environment. |

**Dependabot: version bumps vs security**

- **Version updates** — Dependabot checks on the weekly schedule; each proposed bump must pass the **10-day cooldown** (release age). dep-updater usually lands the same bump first (9-day gate); Dependabot version PRs after that are redundant and can be closed.
- **Security updates** — **not** subject to the version-update cooldown. Dependabot may open a security PR as soon as GitHub has an alert and a fix; merge these promptly.
- **dep-updater CVE bypass** — when **npm** or **Python** audit reports CVE IDs with an available fix, dep-updater skips the 9-day gate for that package only (`--security-only` mode is available).

**Day-to-day:** merge dep-updater batch PRs for routine bumps; close duplicate Dependabot version PRs when dep-updater already has the change. Re-run `scripts/grandfather-pnpm-release-age --wildcard` on `web/` only if `pnpm-workspace.yaml` was lost after a major lockfile reset.

**`.github/CODEOWNERS`** maps `*` to `@thehcma` (blanket ownership for now). Adding additional reviewers later is a one-line entry per path glob.

No PR may be merged with a failing CI check.

---

## Pre-Commit Checklist

Before every commit (mirrors the CI gates above; `uv sync --group dev` when deps changed):

- [ ] `uv run pyright` — passes with no new errors
- [ ] `uv run pytest -m "not integration and not browser" -n auto` — green (or single-process without `-n auto` when debugging)
- [ ] If `app/api/static/index.html` or browser tests changed: `uv run pytest -m browser` — green (Playwright Chromium installed)
- [ ] `shellcheck` clean on any modified shell scripts
- [ ] `actionlint` clean on any modified workflow files (`uvx actionlint` or the binary)
- [ ] If any `web/` source changed: `cd web && pnpm run check` (typecheck + build) is green
- [ ] If any `app/api/static/icons/compact/*.svg` changed: `./scripts/internal/generate-compact-icon-preview` and attach `$HOME/scratch/domesti-bot/tmp/build/review.html` (or a screenshot) to the PR
- [ ] No trailing whitespace; empty lines have no whitespace
- [ ] Imports sorted; all imports at module level
- [ ] New methods / module-level functions inserted in **lexicographic** position (public block, then private block — see `python-sorted-methods.mdc`)
- [ ] No `print()` in library code (use `logging`)
- [ ] No hardcoded credentials or well-known ports in tests
- [ ] Commit message follows Conventional Commits and is GPG-signed

No PR may be submitted with any of the above failing.
