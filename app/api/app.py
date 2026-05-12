"""FastAPI ASGI app: device discovery and REPL-equivalent actions over HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, Response

from app.api.schemas import CompletionAliasesOut, ExecuteLineIn, ExecuteLineOut
from app.device_manager_cli import (
    DeviceManagersState,
    _Theme,
    _all_cli_device_labels,
    _media_playback_aliases,
    _switch_aliases,
    _tailwind_door_aliases,
    bootstrap_device_managers,
    execute_line_for_api,
    shutdown_device_managers,
)


_LOGGER = logging.getLogger("app.api")


_LANDING_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>domesti-bot</title>
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    :root {
      color-scheme: light dark;
      --fg: #1d1f21;
      --muted: #5a6171;
      --accent: #2e7d32;
      --card: #ffffff;
      --bg: #f6f7f9;
      --border: #e1e4e8;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --fg: #e6e6e6;
        --muted: #a0a6b1;
        --accent: #7bd389;
        --card: #1f2226;
        --bg: #15171a;
        --border: #2a2e34;
      }
    }
    html, body { margin: 0; padding: 0; background: var(--bg); color: var(--fg);
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; }
    main { max-width: 640px; margin: 6vh auto; padding: 24px;
      background: var(--card); border: 1px solid var(--border); border-radius: 8px; }
    h1 { margin: 0 0 8px; font-size: 20px; }
    .ok { color: var(--accent); font-weight: 600; }
    p { margin: 8px 0; color: var(--muted); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      font-size: 13px; background: var(--bg); border: 1px solid var(--border);
      border-radius: 4px; padding: 1px 6px; }
    ul { margin: 12px 0 0; padding-left: 20px; }
    li { margin: 4px 0; }
    a { color: inherit; }
  </style>
</head>
<body>
  <main>
    <h1><span class="ok">success</span> &mdash; domesti-bot is running</h1>
    <p>The HTTP API is up. There is no browser UI; this service exposes JSON
       endpoints for the REPL and external integrations.</p>
    <ul>
      <li><a href="/health"><code>GET /health</code></a> &mdash; liveness probe</li>
      <li><a href="/v1/completion-aliases"><code>GET /v1/completion-aliases</code></a> &mdash; tab-completion data</li>
      <li><code>POST /v1/execute-line</code> &mdash; run a REPL command line</li>
    </ul>
  </main>
</body>
</html>
"""


class _AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit a single ``[http]`` log line per request.

    The :class:`app.logging_config.HealthCheckFilter` demotes ``/health`` lines
    to TRACE so they don't pollute INFO output. Each record looks like::

        [http] 127.0.0.1 GET /v1/completion-aliases 200 (12.3 ms)
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:  # noqa: D401
        started = time.perf_counter()
        client_host = request.client.host if request.client else "-"
        try:
            response = await call_next(request)
        except Exception:
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            _LOGGER.exception(
                "[http] %s %s %s 500 (%.1f ms)",
                client_host,
                request.method,
                request.url.path,
                elapsed_ms,
            )
            raise
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        _LOGGER.info(
            "[http] %s %s %s %d (%.1f ms)",
            client_host,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


def _expected_api_key() -> str:
    return (os.environ.get("DOMESTI_API_KEY") or "").strip()


async def _verify_api_key(
    x_domesti_api_key: Annotated[str | None, Header(alias="X-Domesti-Api-Key")] = None,
) -> None:
    expected = _expected_api_key()
    if not expected:
        return
    if (x_domesti_api_key or "").strip() != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Domesti-Api-Key")


def _device_state(request: Request) -> DeviceManagersState:
    st: Any = getattr(request.app.state, "device_state", None)
    if st is not None:
        return st
    err: str | None = getattr(request.app.state, "discovery_error", None)
    if err is not None:
        raise HTTPException(
            status_code=503,
            detail=f"Device discovery failed: {err}",
            headers={"Retry-After": "30"},
        )
    raise HTTPException(
        status_code=503,
        detail="Device discovery still in progress; check /health and retry shortly",
        headers={"Retry-After": "2"},
    )


DeviceState = Annotated[DeviceManagersState, Depends(_device_state)]
Auth = Annotated[None, Depends(_verify_api_key)]


def create_app(args: Any) -> FastAPI:
    """Build the app; ``args`` is the same :class:`argparse.Namespace` as the REPL CLI."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The HTTP server must accept connections as soon as the ASGI lifespan
        # yields. Device discovery (Cast mDNS, Sonos UDP, Kasa LAN sweep) can
        # take 20+ seconds on a cold cache, so we run it as a background task
        # and let ``_device_state`` short-circuit dependent routes with a 503
        # while it's in flight. Static routes (``/``, ``/health``,
        # ``/favicon.ico``) remain responsive throughout.
        theme = _Theme(enabled=False)
        app.state.device_state = None
        app.state.discovery_error = None
        app.state.discovery_started_at = time.monotonic()
        app.state.discovery_completed_at = None

        async def _run_discovery() -> None:
            started = time.monotonic()
            _LOGGER.info("[startup] device discovery beginning in background")
            try:
                state = await bootstrap_device_managers(
                    args, theme=theme, log_progress=True
                )
            except asyncio.CancelledError:
                _LOGGER.info("[startup] device discovery cancelled before completing")
                raise
            except Exception as exc:
                app.state.discovery_error = repr(exc)
                _LOGGER.exception("[startup] device discovery failed: %s", exc)
                return
            app.state.device_state = state
            app.state.discovery_completed_at = time.monotonic()
            _LOGGER.info(
                "[startup] device discovery complete in %.1fs",
                app.state.discovery_completed_at - started,
            )

        discovery_task = asyncio.create_task(_run_discovery(), name="device-discovery")
        app.state.discovery_task = discovery_task
        try:
            yield
        finally:
            if not discovery_task.done():
                discovery_task.cancel()
                try:
                    await discovery_task
                except (asyncio.CancelledError, Exception):
                    pass
            state = app.state.device_state
            if state is not None:
                await shutdown_device_managers(state)

    app = FastAPI(
        title="domesti-bot",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_middleware(_AccessLogMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        return HTMLResponse(_LANDING_PAGE_HTML)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Browsers fetch /favicon.ico automatically; return 204 to silence the
        # access log 404 without shipping an actual icon asset.
        return Response(status_code=204)

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        st: Any = getattr(request.app.state, "device_state", None)
        err: str | None = getattr(request.app.state, "discovery_error", None)
        if st is not None:
            discovery = "ready"
        elif err is not None:
            discovery = "failed"
        else:
            discovery = "in_progress"
        return {
            "status": "ok",
            "service": "domesti-bot",
            "ready": st is not None,
            "discovery": discovery,
            "error": err,
        }

    @app.get("/v1/completion-aliases", dependencies=[Depends(_verify_api_key)])
    async def completion_aliases(state: DeviceState) -> CompletionAliasesOut:
        return CompletionAliasesOut(
            switch=_switch_aliases(state.kasa_mgr, state.androidtv_mgr),
            sonos=_media_playback_aliases(state.sonos_mgr),
            tailwind=_tailwind_door_aliases(state.tailwind_mgr),
            all_device_labels=_all_cli_device_labels(
                state.kasa_mgr, state.tailwind_mgr, state.androidtv_mgr
            ),
        )

    @app.post("/v1/execute-line", dependencies=[Depends(_verify_api_key)])
    async def execute_line(body: ExecuteLineIn, state: DeviceState) -> ExecuteLineOut:
        out, err, api_err = await execute_line_for_api(
            state.kasa_mgr,
            state.sonos_mgr,
            state.tailwind_mgr,
            state.androidtv_mgr,
            cache_path=state.cache_path,
            androidtv_zeroconf_timeout=float(state.args.androidtv_zeroconf_timeout),
            line=body.line,
        )
        if api_err:
            return ExecuteLineOut(stdout=out, stderr=err, error=api_err)
        return ExecuteLineOut(stdout=out, stderr=err, error=None)

    return app
