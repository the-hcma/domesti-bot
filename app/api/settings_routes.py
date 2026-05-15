"""HTTP routes for operator settings (encrypted secrets, no device state required)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    TailwindTokenSetIn,
    TailwindTokenSetOut,
    TailwindTokenSettingsOut,
)
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    delete_app_secret,
    load_tailwind_token_from_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    tailwind_token_stored_in_db,
)
from app.domesti_bot_cli import DeviceManagersState, _bootstrap_tailwind, _Theme
from app.tailwind_credentials import resolve_tailwind_token

router = APIRouter(prefix="/v1/settings", tags=["settings"])


def discovery_cache_path_from_request(request: Request) -> Path | None:
    """Resolve the discovery SQLite path from ``app.state.cli_args``."""
    args = getattr(request.app.state, "cli_args", None)
    if args is None:
        return None
    raw = getattr(args, "discovery_cache", None)
    if raw is None:
        return None
    return Path(str(raw)).expanduser().resolve()


@router.delete("/tailwind-token", response_model=TailwindTokenSettingsOut)
async def clear_tailwind_token(request: Request) -> TailwindTokenSettingsOut:
    """Remove the encrypted database token (environment / CLI tokens are unchanged)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot clear stored Tailwind token: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    delete_app_secret(cache_path, key="tailwind_token")
    await _reload_tailwind_manager(request)
    return _tailwind_settings_response(request)


@router.get("/tailwind-token", response_model=TailwindTokenSettingsOut)
async def get_tailwind_token_settings(request: Request) -> TailwindTokenSettingsOut:
    """Return Tailwind credential status (includes stored DB token when present)."""
    return _tailwind_settings_response(request)


@router.put("/tailwind-token", response_model=TailwindTokenSetOut)
async def put_tailwind_token(
    body: TailwindTokenSetIn, request: Request
) -> TailwindTokenSetOut:
    """Encrypt and store the GoTailwind Local Control Key."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Tailwind token: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    token = body.token.strip()
    if not token:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected a non-empty token, got whitespace only",
        )
    try:
        save_tailwind_token_to_db(cache_path, token)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    resolved, source = resolve_tailwind_token(
        cli_token=_cli_tailwind_token(request),
        cache_path=cache_path,
    )
    env_active = source == "env" or source == "cli"
    reload_ok = False
    if not env_active:
        reload_ok = await _reload_tailwind_manager(request)
    return TailwindTokenSetOut(
        configured=bool(resolved),
        source=source,
        restart_required=not env_active and not reload_ok,
    )


def _cli_tailwind_token(request: Request) -> str | None:
    args = getattr(request.app.state, "cli_args", None)
    if args is None:
        return None
    raw = getattr(args, "tailwind_token", None)
    return str(raw) if raw else None


async def _reload_tailwind_manager(request: Request) -> bool:
    """Re-bootstrap GoTailwind on the live server after token storage changes."""
    state: DeviceManagersState | None = getattr(
        request.app.state, "device_state", None
    )
    if state is None:
        return False
    cache_path = discovery_cache_path_from_request(request)
    token, _source = resolve_tailwind_token(
        cli_token=_cli_tailwind_token(request),
        cache_path=cache_path,
    )
    if state.tailwind_mgr is not None:
        await state.tailwind_mgr.disconnect()
    if not token:
        request.app.state.device_state = state._replace(tailwind_mgr=None)
        return False
    mgr, _exc = await _bootstrap_tailwind(
        args=state.args,
        cache_path=cache_path,
        theme=_Theme(enabled=False),
        token=token,
        log_failures=True,
    )
    request.app.state.device_state = state._replace(tailwind_mgr=mgr)
    return mgr is not None


def _stored_token_for_settings(cache_path: Path | None) -> str | None:
    if cache_path is None:
        return None
    try:
        return load_tailwind_token_from_db(cache_path)
    except SecretsDecryptError:
        return None


def _tailwind_settings_response(request: Request) -> TailwindTokenSettingsOut:
    cache_path = discovery_cache_path_from_request(request)
    token, source = resolve_tailwind_token(
        cli_token=_cli_tailwind_token(request),
        cache_path=cache_path,
    )
    stored = (
        tailwind_token_stored_in_db(cache_path)
        if cache_path is not None
        else False
    )
    return TailwindTokenSettingsOut(
        configured=bool(token),
        source=source,
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        stored_in_database=stored,
        stored_token=_stored_token_for_settings(cache_path) if stored else None,
    )
