"""FastAPI ASGI app: device discovery and REPL-equivalent actions over HTTP."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
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

# ``app/api/static/`` ships the landing page and (after ``pnpm run build``) the
# compiled JS bundle under ``dist/``. The directory itself is committed (with a
# ``.gitkeep``) so the FastAPI mount succeeds even when the bundle has not been
# built yet; the ``dist/`` subdirectory is gitignored.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_LANDING_PAGE_PATH = _STATIC_DIR / "index.html"


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

    # Serve the static landing page + the compiled TypeScript bundle from
    # ``app/api/static/`` at ``/static/``. Mounted unconditionally — the
    # directory always exists even when ``dist/`` is empty (CI / a fresh clone
    # before ``pnpm run build``); broken ``<script>`` references just 404, the
    # rest of the page still renders.
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> HTMLResponse:
        # Read from disk on every request so dev-mode edits to ``index.html``
        # show up without restarting the server. The file is small (~1.5 KB)
        # so the I/O cost is negligible.
        try:
            html = _LANDING_PAGE_PATH.read_text(encoding="utf-8")
        except FileNotFoundError:
            _LOGGER.exception(
                "[index] landing page missing at %s", _LANDING_PAGE_PATH
            )
            raise HTTPException(
                status_code=500,
                detail=f"Landing page missing at {_LANDING_PAGE_PATH}",
            )
        return HTMLResponse(html)

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
