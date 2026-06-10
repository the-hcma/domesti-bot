"""FastAPI ASGI app: device discovery and REPL-equivalent actions over HTTP."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from contextlib import asynccontextmanager
from http import HTTPStatus
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, HTMLResponse, Response

from app import kasa_discovery_store
from app.logging_config import TRACE_LEVEL
from app.api.schemas import (
    CompletionAliasesOut,
    ExecuteLineIn,
    ExecuteLineOut,
    MetaOut,
    UIBulkActionOut,
    UIDeviceActionOut,
    UIGlobalBulkActionItem,
    UIGlobalBulkActionOut,
    UIPowerSetIn,
    UIPreferenceIn,
    UIPreferenceOut,
    UISonosSetIn,
    UIStateOut,
)
from app.api.settings_routes import router as settings_router
from app.api.mytracks_routes import rules_router as mytracks_rules_router
from app.api.mytracks_routes import settings_router as mytracks_settings_router
from app.api.location_update_routes import router as location_update_router
from app.api.rules_routes import router as rules_router
from app.api.webhooks_routes import router as webhooks_router
from app.api.smtp_routes import router as smtp_router
from app.api.ui_state import (
    build_kasa_device_view,
    build_sonos_device_view,
    build_tailwind_device_view,
    build_ui_state,
    bulk_close_tailwind_apply,
    bulk_off_global_apply,
    bulk_off_kasa_apply,
    bulk_pause_sonos_apply,
    find_kasa_by_host,
    find_sonos_by_identifier,
    find_tailwind_by_identifier,
)
from app.build_info import get_build_info
from app.device_state_watcher import (
    build_default_watchers,
    poll_interval_from_env,
    run_device_state_watchers,
)
from app.domesti_bot_cli import (
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
from app.sonos_device_manager import SonosTransitionUnavailableError


_LOGGER = logging.getLogger("app.api")

# ``app/api/static/`` ships the landing page and (after ``pnpm run build``) the
# compiled JS bundle under ``dist/``. The directory itself is committed (with a
# ``.gitkeep``) so the FastAPI mount succeeds even when the bundle has not been
# built yet; the ``dist/`` subdirectory is gitignored.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_LANDING_PAGE_PATH = _STATIC_DIR / "index.html"


# Paths whose *successful* (HTTP < 400) access log lines are emitted
# at TRACE instead of DEBUG. These are the highest-frequency polling
# endpoints whose [http] records would otherwise dominate DEBUG
# output. Failure responses (>= 400) for the same paths still log at
# INFO so genuine errors stay visible at the default level.
#
# This complements :class:`app.logging_config.HealthCheckFilter`,
# which post-hoc demotes ``/health`` lines all the way to TRACE as
# well. All other successful requests log at DEBUG (below INFO).
_QUIET_ACCESS_LOG_PATHS: frozenset[str] = frozenset({
    # The web UI polls this on a 5s cadence (see ``main.ts``).
    "/v1/ui/state",
})


class _AccessLogMiddleware(BaseHTTPMiddleware):
    """Emit a single ``[http]`` log line per request.

    Level selection:

    * 5xx raised exceptions → ``logger.exception(...)`` (ERROR with traceback).
    * 4xx/5xx responses     → INFO (errors should stay visible at the default level).
    * Successful responses to paths in :data:`_QUIET_ACCESS_LOG_PATHS` → TRACE
      (poll heartbeats; see the constant's docstring).
    * Other successful responses → DEBUG (routine client traffic stays below INFO).

    The :class:`app.logging_config.HealthCheckFilter` demotes ``/health`` lines
    further to TRACE so they never surface even at DEBUG. Each record looks like::

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
        if response.status_code >= 400:
            level = logging.INFO
        elif request.url.path in _QUIET_ACCESS_LOG_PATHS:
            level = TRACE_LEVEL
        else:
            level = logging.DEBUG
        _LOGGER.log(
            level,
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
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="Invalid or missing X-Domesti-Api-Key",
        )


def _device_state(request: Request) -> DeviceManagersState:
    st: Any = getattr(request.app.state, "device_state", None)
    if st is not None:
        return st
    err: str | None = getattr(request.app.state, "discovery_error", None)
    if err is not None:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=f"Device discovery failed: {err}",
            headers={"Retry-After": "30"},
        )
    raise HTTPException(
        status_code=HTTPStatus.SERVICE_UNAVAILABLE,
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
        # Continuous state watcher (kicked off after discovery succeeds —
        # see ``app.device_state_watcher``). The lifespan owns the stop
        # event so it can shut watchers down cleanly before tearing down
        # the underlying managers.
        watcher_stop = asyncio.Event()
        app.state.watcher_stop = watcher_stop
        app.state.watcher_task = None

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
            try:
                poll_interval_s = poll_interval_from_env()
            except ValueError as exc:
                _LOGGER.error(
                    "[state-watcher] disabled — bad DOMESTI_STATE_POLL_INTERVAL_S: %s",
                    exc,
                )
                return
            watchers = build_default_watchers(state, interval_s=poll_interval_s)
            app.state.watcher_task = asyncio.create_task(
                run_device_state_watchers(watchers, stop=watcher_stop),
                name="device-state-watcher",
            )
            _LOGGER.info(
                "[state-watcher] started; polling every %.1fs across %d backend(s)",
                poll_interval_s,
                len(watchers),
            )

        discovery_task = asyncio.create_task(_run_discovery(), name="device-discovery")
        app.state.discovery_task = discovery_task
        try:
            yield
        finally:
            if not discovery_task.done():
                discovery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await discovery_task
            # Stop the state watcher *before* tearing down managers so we
            # don't poll a half-disconnected backend during shutdown.
            watcher_stop.set()
            watcher_task: asyncio.Task[None] | None = getattr(
                app.state, "watcher_task", None
            )
            if watcher_task is not None and not watcher_task.done():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher_task
            state = app.state.device_state
            if state is not None:
                await shutdown_device_managers(state)

    app = FastAPI(
        title="domesti-bot",
        version=get_build_info()[0],
        lifespan=lifespan,
    )
    app.state.cli_args = args
    app.include_router(settings_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(smtp_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(mytracks_settings_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(mytracks_rules_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(location_update_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(rules_router, dependencies=[Depends(_verify_api_key)])
    app.include_router(webhooks_router)
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
                status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
                detail=f"Landing page missing at {_LANDING_PAGE_PATH}",
            )
        return HTMLResponse(html)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        # Browsers fetch /favicon.ico automatically; return 204 to silence the
        # access log 404 without shipping an actual icon asset.
        return Response(status_code=HTTPStatus.NO_CONTENT)

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

    @app.get("/sw.js", include_in_schema=False)
    async def service_worker() -> FileResponse:
        # Served at the site root so ``navigator.serviceWorker.register`` can use
        # ``{ scope: '/' }``. A file under ``/static/`` would default to ``/static/``
        # scope only unless every response carried ``Service-Worker-Allowed``.
        path = _STATIC_DIR / "sw.js"
        if not path.is_file():
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Service worker missing at {path}",
            )
        return FileResponse(
            path,
            media_type="application/javascript",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/v1/meta", response_model=MetaOut)
    async def meta() -> MetaOut:
        ver, commit = get_build_info()
        return MetaOut(version=ver, commit=commit)

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

    @app.post(
        "/v1/ui/global/bulk-off",
        dependencies=[Depends(_verify_api_key)],
    )
    async def global_bulk_off(state: DeviceState) -> UIGlobalBulkActionOut:
        # Global "turn off / close everything" — kasa devices get
        # ``turn_off``, tailwind doors get ``close``. ``exclude_from_global=True``
        # rows are honored (they appear in ``skipped``).
        affected, skipped = await bulk_off_global_apply(
            state, cache_path=state.cache_path
        )
        return UIGlobalBulkActionOut(
            affected=[
                UIGlobalBulkActionItem(family_id=fam, device_id=dev)
                for fam, dev in affected
            ],
            skipped=[
                UIGlobalBulkActionItem(family_id=fam, device_id=dev)
                for fam, dev in skipped
            ],
        )

    @app.post(
        "/v1/ui/kasa/bulk-off",
        dependencies=[Depends(_verify_api_key)],
    )
    async def kasa_bulk_off(state: DeviceState) -> UIBulkActionOut:
        # Family-level bulk: ``exclude_from_global`` is intentionally
        # **not** consulted (the user explicitly clicked "all kasa off").
        affected, skipped = await bulk_off_kasa_apply(state)
        return UIBulkActionOut(affected=affected, skipped=skipped)

    @app.post(
        "/v1/ui/kasa/devices/{device_id}/toggle",
        dependencies=[Depends(_verify_api_key)],
    )
    async def kasa_set_power(
        device_id: str,
        body: UIPowerSetIn,
        state: DeviceState,
    ) -> UIDeviceActionOut:
        kd = find_kasa_by_host(state.kasa_mgr, device_id)
        if kd is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Unknown kasa device: {device_id}",
            )
        if body.on:
            await kd.turn_on()
        else:
            await kd.turn_off()
        return UIDeviceActionOut(
            device=build_kasa_device_view(
                state.kasa_mgr, host=device_id, cache_path=state.cache_path
            )
        )

    @app.put(
        "/v1/ui/preferences/{family_id}/{device_id}",
        dependencies=[Depends(_verify_api_key)],
    )
    async def set_ui_preference(
        family_id: str,
        device_id: str,
        body: UIPreferenceIn,
        state: DeviceState,
    ) -> UIPreferenceOut:
        if family_id not in {"kasa", "sonos", "tailwind"}:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"Unknown family_id: {family_id}",
            )
        if state.cache_path is None:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    "Per-device UI preferences cannot be persisted: server "
                    "started with --no-discovery-cache. Restart with a "
                    "discovery cache path to enable the exclude-from-global "
                    "checkbox."
                ),
            )
        if family_id == "kasa" and find_kasa_by_host(state.kasa_mgr, device_id) is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Unknown kasa device: {device_id}",
            )
        if family_id == "sonos":
            son = state.sonos_mgr
            if son is None or find_sonos_by_identifier(son, device_id) is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=f"Unknown sonos device: {device_id}",
                )
        if family_id == "tailwind":
            tw = state.tailwind_mgr
            if tw is None or all(d.identifier != device_id for d in tw.doors):
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=f"Unknown tailwind device: {device_id}",
                )
        kasa_discovery_store.upsert_ui_preference(
            state.cache_path,
            backend=family_id,
            canonical_key=device_id,
            exclude_from_global=body.exclude_from_global,
        )
        return UIPreferenceOut(
            family_id=family_id,
            device_id=device_id,
            exclude_from_global=body.exclude_from_global,
        )

    @app.post(
        "/v1/ui/sonos/pause-all",
        dependencies=[Depends(_verify_api_key)],
    )
    async def sonos_pause_all(state: DeviceState) -> UIBulkActionOut:
        # Family-level bulk: ignores per-device ``exclude_from_global``.
        # When the Sonos manager is absent (``--no-sonos`` or empty
        # discovery) returns an empty result rather than 404 so the UI
        # can call this unconditionally; the family won't be visible
        # anyway. Already-paused zones drop out without LAN traffic.
        affected, skipped = await bulk_pause_sonos_apply(state)
        return UIBulkActionOut(affected=affected, skipped=skipped)

    @app.post(
        "/v1/ui/sonos/zones/{device_id}/toggle",
        dependencies=[Depends(_verify_api_key)],
    )
    async def sonos_set_playback(
        device_id: str,
        body: UISonosSetIn,
        state: DeviceState,
    ) -> UIDeviceActionOut:
        if state.sonos_mgr is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="Sonos manager is not configured on this server",
            )
        sp = find_sonos_by_identifier(state.sonos_mgr, device_id)
        if sp is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Unknown sonos device: {device_id}",
            )
        try:
            if body.playing:
                await sp.resume(favorite_index=body.favorite_index)
            else:
                await sp.pause()
        except ValueError as exc:
            raise HTTPException(
                status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
        except SonosTransitionUnavailableError as exc:
            # UPnP 701 from Sonos: empty queue, mid-transition, or any
            # other state the zone can't transition out of right now.
            # The device has already refreshed its cached
            # ``is_playing`` from a live UPnP read inside ``pause`` /
            # ``resume``, so the refreshed view below mirrors reality.
            # 409 is the right status here — the request was
            # well-formed but the resource state forbids the action.
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT, detail=str(exc)
            ) from exc
        return UIDeviceActionOut(
            device=build_sonos_device_view(
                state.sonos_mgr,
                device_id=device_id,
                cache_path=state.cache_path,
            )
        )

    @app.post(
        "/v1/ui/tailwind/close-all",
        dependencies=[Depends(_verify_api_key)],
    )
    async def tailwind_close_all(state: DeviceState) -> UIBulkActionOut:
        # Family-level bulk: ignores per-device ``exclude_from_global``.
        # When the tailwind manager is absent (``--no-tailwind``) returns
        # an empty result rather than 404 so the UI can call this
        # unconditionally; the family won't be visible anyway.
        affected, skipped = await bulk_close_tailwind_apply(state)
        return UIBulkActionOut(affected=affected, skipped=skipped)

    @app.post(
        "/v1/ui/tailwind/doors/{device_id}/close",
        dependencies=[Depends(_verify_api_key)],
    )
    async def tailwind_close_door(
        device_id: str,
        state: DeviceState,
    ) -> UIDeviceActionOut:
        if state.tailwind_mgr is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="Tailwind manager is not configured on this server",
            )
        gd = find_tailwind_by_identifier(state.tailwind_mgr, device_id)
        if gd is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Unknown tailwind device: {device_id}",
            )
        await gd.close()
        return UIDeviceActionOut(
            device=build_tailwind_device_view(
                state.tailwind_mgr,
                device_id=device_id,
                cache_path=state.cache_path,
            )
        )

    @app.post(
        "/v1/ui/tailwind/doors/{device_id}/open",
        dependencies=[Depends(_verify_api_key)],
    )
    async def tailwind_open_door(
        device_id: str,
        state: DeviceState,
    ) -> UIDeviceActionOut:
        if state.tailwind_mgr is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail="Tailwind manager is not configured on this server",
            )
        gd = find_tailwind_by_identifier(state.tailwind_mgr, device_id)
        if gd is None:
            raise HTTPException(
                status_code=HTTPStatus.NOT_FOUND,
                detail=f"Unknown tailwind device: {device_id}",
            )
        await gd.open()
        return UIDeviceActionOut(
            device=build_tailwind_device_view(
                state.tailwind_mgr,
                device_id=device_id,
                cache_path=state.cache_path,
            )
        )

    @app.get("/v1/ui/state", dependencies=[Depends(_verify_api_key)])
    async def ui_state(state: DeviceState) -> UIStateOut:
        # Read-only join of in-memory manager state with the persisted
        # ``ui_preferences`` SQLite rows.
        return build_ui_state(state, cache_path=state.cache_path)

    return app
