# AGENTS.md — Ground Rules for domesti-bot

This file defines the non-negotiable standards for all contributors (human or AI) working on this codebase. Every change must comply with these rules before it is considered complete.

---

## Session Startup

- **At the start of every session, before any other work**, run both commands in order ([repository-helpers](https://github.com/the-hcma/repository-helpers)):
  ```
  ~/work/ai/repository-helpers/scripts/dev/start-development --refresh
  ~/work/ai/repository-helpers/scripts/dev/start-development
  ```
  - **`--refresh`** (first): syncs `main` with Graphite (`gt sync`), prunes merged worktrees and branches, pulls latest `main`, and ensures the systemd service (`domesti-bot-server.service`) is installed and running. Exits immediately — it does **not** prompt for a worktree.
  - **plain** (second): repeats the sync/cleanup, then prompts you to name a new worktree for the upcoming work. Pass `--worktree <name> --no-interactive` to skip the prompt.
- Both commands are required. This replaces any manual `gt sync --force` step.

---

## Language & Runtime

- **Python 3.14** is the target runtime (`.python-version`). `pyproject.toml` declares `requires-python = ">=3.11"` as the compatibility floor.
- Use **modern Python typing** — built-in generics, not the `typing` module equivalents:
  - `list[str]`, `dict[str, Any]`, `tuple[str, ...]`, `set[int]`, `str | None`
  - ❌ `List[str]`, `Dict[str, Any]`, `Optional[str]`
  - Only import from `typing` what you actually need (`Any`, `Annotated`, `cast`, `TypeVar`, `TYPE_CHECKING`).
- Every new module starts with `from __future__ import annotations` (matches the rest of the codebase).
- Every public function and method has complete type annotations (parameters and return type). `pyright` enforces this.

---

## Package Management

- **Only `uv`** is used for Python package management. Never `pip` directly.
- Install / sync dependencies: `uv sync` (add `--all-extras` if/when extras are introduced).
- Add a dependency: `uv add <pkg>` (separates runtime from dev correctly). Do not hand-edit `pyproject.toml` for additions.
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
│   ├── kasa_discovery_store.py            SQLite discovery cache (shared by all)
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
├── production/                           Server-side deploy bits
│   └── systemd/domesti-bot-server.service.template
├── web/                                  Browser TypeScript bundle (see "Web UI" below)
│   ├── package.json                       pnpm scripts; pinned via `packageManager`
│   ├── pnpm-lock.yaml                     committed; `pnpm install --frozen-lockfile`
│   ├── tsconfig.json                      strict TS + Bundler module resolution
│   ├── build.mjs                          one esbuild call → app/api/static/dist/main.js
│   └── src/main.ts                        browser entrypoint
├── app/api/static/                       Files served at `/static/` by FastAPI
│   ├── index.html                         landing page (loads /static/dist/main.js)
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
- The HTTP API is a subpackage `app/api/` (parallel to how my-tracks organizes `app/mqtt/`). Do not flatten Pydantic schemas into `app/` proper.
- `config/` holds only process-level wiring (uvicorn launch, future settings glue). It is **not** a place for domain code.
- **Never move** `pyproject.toml`, `pyrightconfig.json`, `uv.lock`, or `.python-version` out of the repo root — IDE and tool discovery walk up from source files and depend on root-level placement.
- Tests live in `tests/python/` (mirroring my-tracks). Real-hardware integration tests live alongside unit tests but carry the `@pytest.mark.integration` marker. Static fixture data lives in `tests/python/fixtures/`.
- `tests/bash/` is reserved for future shell-script tests. Keep `.gitkeep` until real tests exist.
- **Browser code lives in `web/`** (TypeScript). Sources never go in `app/api/static/` — only the build output (`app/api/static/dist/`) does, and that is gitignored. See "Web UI" below.

---

## Code Style

- **Sorted methods and module-level functions** — enforced by `.cursor/rules/python-sorted-methods.mdc`. Within each `class`, define methods and `@property` getters in ASCII alphabetical order by name (the name after `def`). Treat `async def` like `def`. Same rule for module-level functions. Dunder methods (`__init__`, `__str__`, …) participate in the same sort. **Insert** new APIs in sorted position rather than appending at the bottom.
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
- **HTTP status codes use `http.HTTPStatus`, not integer literals.** Both server code (`HTTPException(status_code=HTTPStatus.NOT_FOUND, ...)`, `Response(status_code=HTTPStatus.NO_CONTENT)`) and tests (`assert response.status_code == HTTPStatus.OK`) reference the named constants. `HTTPStatus` is an `IntEnum`, so it compares equal to the integer codes — wire format is unchanged. Common values used in this repo: `OK` (200), `NO_CONTENT` (204), `BAD_REQUEST` (400), `UNAUTHORIZED` (401), `NOT_FOUND` (404), `CONFLICT` (409), `UNPROCESSABLE_ENTITY` (422), `INTERNAL_SERVER_ERROR` (500), `SERVICE_UNAVAILABLE` (503).

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

Run the suite:
```
uv run pytest -m "not integration"     # fast / hermetic
uv run pytest                          # full suite
uv run pytest -m integration           # LAN hardware only
```

---

## HTTP API

- The FastAPI app is created via `app.api.app.create_app(args)`; the entrypoint is `config/serve.py` (run as `python -m config.serve`, or via `scripts/domesti-bot-server`).
- **Authentication**: when `DOMESTI_API_KEY` is set in the environment, every protected endpoint requires the `X-Domesti-Api-Key` header. If the env var is unset, the API is open (intended for trusted LAN only — never expose unauthenticated to the public internet).
- **Bind address**: production systemd binds to `127.0.0.1:8765`. Do not change the production unit to `0.0.0.0` without a fronting reverse proxy that handles TLS and auth.
- **Dev-mode default** (no flags, no env vars): bind to `127.0.0.1` on an **OS-allocated free port** (mirrors `fpdf`'s launcher). The startup banner logs `[http] listening on http://127.0.0.1:<port> (api-key …)` so the developer can paste the URL into a browser. The launcher pre-binds the socket with `config.serve.bind_listen_socket()` *before* lifespan / device discovery runs, so the URL appears at the top of the run rather than after the discovery wait. Use `--listen-port 8765` or `DOMESTI_LISTEN_PORT=8765` to pin a specific port. Precedence: CLI flag → env var → dev default.
- **CORS**: the dev configuration uses `allow_origins=["*"]`. Tighten this before exposing the service outside the LAN.
- **Pydantic schemas** for all request and response bodies live in `app/api/schemas.py`. New endpoints must define typed `*In` / `*Out` models — no raw `dict[str, Any]` return types.
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
- **Landing page.** `GET /` reads `app/api/static/index.html` from disk on every request (so dev edits show up without restarting the server) and returns it as `HTMLResponse`, `include_in_schema=False`. The HTML loads `/static/dist/main.js` (the compiled TypeScript bundle) and exposes `<span id="bundle-status">` so the bundle can flip its `data-state` from `pending` to `ready` to confirm it executed. The page is safe to hit before discovery completes (no `_device_state` dependency).
- **Static assets.** `app/api/static/` is mounted at `/static/` via `StaticFiles`. Source files (HTML, future CSS) live there directly and are committed; the `dist/` subdirectory is gitignored and rebuilt by `pnpm run build` (see "Web UI" below). The mount is unconditional — a missing `dist/main.js` 404s cleanly without breaking `/`.
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

- **No `.sh` extension.** Shell scripts have no file extension. Examples: `scripts/domesti-bot`, `scripts/domesti-bot-server`, `scripts/on-deploy`. The shebang line declares the interpreter.
- Use `#!/usr/bin/env bash` and `set -euo pipefail` at the top of every script.
- **`shellcheck`** is mandatory for all shell scripts. Run `shellcheck <script>` before committing; resolve every finding (or annotate the line with `# shellcheck disable=SCxxxx` plus a comment explaining why).
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

- Standalone CLI scripts have **no `.py` extension** (kebab-case names like `scripts/domesti-bot`).
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

The strategy mirrors `my-tracks` exactly so tail / grep recipes transfer between the two projects.

**Library code**

- Use the stdlib `logging` module — module-level `_LOGGER = logging.getLogger(__name__)`. Never `print()` from library code.
- Prefer **one log record per event** with a single formatted message. Avoid adjacent `logger.*` calls for the same event.
- For exceptions, prefer `logger.exception("...")` or `logger.error("...", exc_info=True)` — do not pair an `error()` with a separate `exception()` for the same failure.
- When the formatted message is expensive and the level may be disabled, guard it: `if _LOGGER.isEnabledFor(logging.DEBUG): ...`.
- Per-request HTTP access logs are emitted by the FastAPI middleware in `app/api/app.py` (tag `[http]`). Do not log the same request a second time inside handlers.
- Use **transport tags** at the start of the message for client-activity records: `[http]`, future `[http-tls]`, `[ws]`. Reserve plain (untagged) lines for internal lifecycle events (startup, shutdown, discovery results).

**Configuration**

- The dict-config and the `LocalTimeFormatter` live in `app/logging_config.py`. The launcher exports env vars; the Python process calls `apply_logging_from_env()` before uvicorn boots.
- Format (identical to my-tracks): `YYYYMMDD-HH:MM:SS.mmm | LEVEL    | module       | message`.
- Timestamps render in the **system timezone** by default. Set `LOG_UTC=1` (or pass `--log-utc`) to switch to UTC.
- Custom **`TRACE`** level (numeric value 5, below `DEBUG`) is auto-registered for high-volume per-request lines. The `HealthCheckFilter` demotes `/health` access lines to `TRACE` so they never pollute `INFO` output.
- **File logging**: when `LOG_FILE` is set (default `$HOME/scratch/domesti-bot/domesti-bot.log` — same per-user scratch-tree convention as `my-tracks`'s `$HOME/scratch/my-tracks/my-tracks.log`), a `RotatingFileHandler` writes 10 MB files with 5 backups. `--no-log-file` disables file output entirely.
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

- **Never log, store, or transmit credentials** (`KASA_PASSWORD`, `TAILWIND_TOKEN`, `DOMESTI_API_KEY`) in plain text. Read them from the environment; do not echo them to stdout or commit `.env` files.
- **Never bind the production server to `0.0.0.0`** without explicit user approval and an auth/TLS plan. The default is `127.0.0.1:8765`.
- **Validate user-controlled paths** (REPL filenames, future upload endpoints) with `pathlib.Path.resolve()` before any filesystem operation; reject paths that escape the working directory.
- **No `eval`, `exec`, or `subprocess.run(..., shell=True)`** with user-controlled strings.
- **Passwords / tokens never appear in shell command arguments** — they end up in `~/.bash_history` and in `ps aux`. Pass them via stdin, environment variables loaded from root-readable files, or systemd `EnvironmentFile=`.

---

## Repository

- Remote: `https://github.com/the-hcma/domesti-bot.git` (private).
- Do not make the repository public without explicit user approval.
- Never commit secrets, credentials, or API keys — use environment variables / `EnvironmentFile=` for systemd.

---

## Commits, Stacking & Pull Requests

> When `docs/GRAPHITE.md` lands in this repo, treat it as the full reference. Until then, follow the conventions below (consistent with the other `the-hcma/*` repos).

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

1. **Pre-PR quality gates** — all must pass locally before submit (these mirror the CI jobs in `.github/workflows/ci.yml`):
   ```
   uv run pyright                          # type errors over app/, config/, scripts/, tests/
   uv run pytest -m "not integration"      # hermetic tests under tests/python/
   shellcheck $(git ls-files scripts | grep -Ev '\.(py|md|txt|yml|yaml|json|toml)$')
   uv run --with pip-audit pip-audit       # CVE check (daily in CI; nice locally too)
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
7. **Merge**: add the `merge-it` label via `gh pr edit <pr> --add-label merge-it`. **Never** run `gh pr merge` directly. **Always ask the user for explicit confirmation before adding the merge label** — it triggers an automated merge.

### Single Responsibility per PR

- Each PR addresses **one concern** (one feature, one bug fix, one refactor). Do not mix unrelated changes.
- The PR title and description must accurately describe that single concern. If the work has grown to multiple concerns, split into separate stacked PRs.
- PR descriptions follow the **Summary + Test plan** format at minimum.

---

## Server Management (development)

- The server runs as a **systemd service** in production (`domesti-bot-server.service`), installed via `~/work/ai/repository-helpers/scripts/setup-service` against the template at `production/systemd/domesti-bot-server.service.template`.
- **During development / testing**: do not start the production server manually. The session-init script (`start-development --refresh`) ensures the background service is running.
- Manual runs for debugging: `./scripts/domesti-bot-server` (forwards all flags to `python -m config.serve`).
- **Do not curl/HTTP against the running production port (8765) during automated testing.** Tests must exercise the ASGI app directly via `httpx.AsyncClient(app=app)` so they cannot collide with the live server.

### Discovery Cache (cache-first startup)

Device discovery is **cache-first**: the LAN probe runs only when the SQLite discovery cache (`$HOME/.cache/rule-engine/device_discovery.sqlite` by default; override with `--discovery-cache`) is empty for that backend or the cached state fails to reconnect. Pass `--force-discovery` to bypass the cache for all backends.

The cache schema lives in `app/kasa_discovery_store.py` (one SQLite file, one table per backend; the module name is historical). All schema changes are **additive only** via `CREATE TABLE IF NOT EXISTS` — `ensure_schema()` is idempotent on legacy files.

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

When adding a new backend, follow the same pattern: a dedicated table, a `load_<backend>()` and `save_<backend>()` pair in `kasa_discovery_store.py`, a `discovery_cache_path` + `force_discovery` pair on the manager constructor, a cache-first branch at the top of `fetch()`, **and** a `last_discovery_source` signal set in both branches of `fetch()` so the bootstrap line is accurate.

### Deploy hook (`scripts/on-deploy`)

`setup-service` from `repository-helpers` calls `scripts/on-deploy` before starting or restarting the systemd unit. The path and contract match `my-tracks/scripts/on-deploy` so the shared tool works against both projects without per-project configuration.

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
5. Reset a stuck `domesti-bot-server.service` failed state (best-effort; ignored if the unit isn't installed yet or sudo isn't available).
6. Compare `git rev-parse HEAD` against the per-host SHA cache (`$HOME/scratch/domesti-bot/on-deploy-sha`, overridable via `ON_DEPLOY_SHA_FILE`). If equal and `--force` was not passed → exit `1`.
7. `uv sync --frozen` — refuse to mutate `uv.lock` on a deploy box; build either matches the committed pin or fails loudly.
8. Smoke-import `config.serve` so a broken dep or syntax error fails the hook BEFORE `setup-service` restarts the unit.
9. Record the deployed SHA, exit `0`.

**Manual runs**: `./scripts/on-deploy` (idempotent) or `./scripts/on-deploy --force` to bypass the SHA cache. `--help` prints the header docblock.

---

## Data Safety

- **Never delete, modify, or purge user data without explicit user approval.** This includes the SQLite discovery cache (`kasa_discovery_store`), Kasa credentials in keystores, and any rule-engine persisted state.
- **Always ask before** running destructive operations (cache wipes, schema migrations that drop columns, etc.).
- If a fix requires deletion, present the impact first ("This will drop 14 cached AndroidTV hosts. Proceed?").

---

## CI Checks (all must pass)

CI lives in `.github/workflows/`:

- **`ci.yml`** — runs on every PR (skipping merge-queue staging branches and already-merged PRs):
  - `Pyright` — `uv run pyright`
  - `Pytest (hermetic)` — `uv run pytest -m "not integration"`
  - `Shellcheck` — every no-extension script under `scripts/`
  - `Web (typecheck + build)` — `pnpm install --frozen-lockfile`, `pnpm run typecheck`, `pnpm run build`, asserts `app/api/static/dist/main.js` exists
  - `Workflow Lint (actionlint)` — validates the YAML in `.github/workflows/`
  - `Secret Scan` — `gitleaks` on the PR diff (full repo on schedule)
- **`cve-check.yml`** — `pip-audit --strict` daily at 08:00 UTC against the synced uv environment.
- **`cleanup-branch-on-merge.yml`** — deletes the head branch when a PR is merged.
- **`cleanup-merged-branches.yml`** — daily sweep for stragglers (merged or closed >30 days).
- **`merged-pr-closer.yml`** — closes open PRs whose changes have already landed on `main` (handles Graphite merge-queue cases where child PRs are left open).
- **`dependabot-auto-merge.yml`** — auto-labels Dependabot PRs with `merge-it`.

No PR may be merged with a failing CI check.

---

## Pre-Commit Checklist

Before every commit (mirrors the CI gates above):

- [ ] `uv run pyright` — passes with no new errors
- [ ] `uv run pytest -m "not integration"` — green, no warnings
- [ ] `shellcheck` clean on any modified shell scripts
- [ ] `actionlint` clean on any modified workflow files (`uvx actionlint` or the binary)
- [ ] If any `web/` source changed: `cd web && pnpm run check` (typecheck + build) is green
- [ ] No trailing whitespace; empty lines have no whitespace
- [ ] Imports sorted; all imports at module level
- [ ] New methods / module-level functions inserted in **alphabetical** position
- [ ] No `print()` in library code (use `logging`)
- [ ] No hardcoded credentials or well-known ports in tests
- [ ] Commit message follows Conventional Commits and is GPG-signed

No PR may be submitted with any of the above failing.
