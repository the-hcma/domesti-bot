"""HTTP routes for operator settings (encrypted secrets, no device state required)."""

from __future__ import annotations

from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import (
    KasaCredentialsSetIn,
    KasaCredentialsSetOut,
    KasaCredentialsSettingsOut,
    TailwindTokenSetIn,
    TailwindTokenSetOut,
    TailwindTokenSettingsOut,
)
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    delete_app_secret,
    delete_kasa_credentials_from_db,
    kasa_credentials_stored_in_db,
    load_kasa_credentials_from_db,
    load_tailwind_token_from_db,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    tailwind_token_stored_in_db,
)
from app.domesti_bot_cli import DeviceManagersState, _bootstrap_tailwind, _Theme
from app.kasa_credentials import resolve_kasa_credentials
from app.server_runtime import runtime
from app.tailwind_credentials import resolve_tailwind_token

router = APIRouter(prefix="/v1/settings", tags=["settings"])


def discovery_cache_path_from_request(request: Request) -> Path | None:
    """Resolve the shared SQLite path for the running server process."""
    del request
    return runtime.discovery_cache_path()


@router.delete("/kasa-credentials", response_model=KasaCredentialsSettingsOut)
async def clear_kasa_credentials(request: Request) -> KasaCredentialsSettingsOut:
    """Remove encrypted Kasa credentials (environment credentials are unchanged).

    Only hot-reloads the live manager when a database row was actually removed,
    so a no-op clear does not wipe in-memory credentials from REPL ``kasa-creds``
    that were never persisted.
    """
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot clear stored Kasa credentials: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    had_stored = kasa_credentials_stored_in_db(cache_path)
    delete_kasa_credentials_from_db(cache_path)
    if had_stored:
        await _reload_kasa_manager()
    return _kasa_settings_response(request)


@router.get("/kasa-credentials", response_model=KasaCredentialsSettingsOut)
async def get_kasa_credentials_settings(request: Request) -> KasaCredentialsSettingsOut:
    """Return Kasa credential status (password is never returned)."""
    return _kasa_settings_response(request)


@router.put("/kasa-credentials", response_model=KasaCredentialsSetOut)
async def put_kasa_credentials(
    body: KasaCredentialsSetIn, request: Request
) -> KasaCredentialsSetOut:
    """Encrypt and store Kasa/Tapo account credentials for KLAP LAN auth."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Kasa credentials: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    try:
        save_kasa_credentials_to_db(
            cache_path,
            username=body.username,
            password=body.password,
        )
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    _creds, source = resolve_kasa_credentials(cache_path=cache_path)
    env_active = source == "env"
    reload_ok = False
    if not env_active:
        reload_ok = await _reload_kasa_manager()
    return KasaCredentialsSetOut(
        configured=_creds is not None,
        source=source,
        restart_required=not env_active and not reload_ok,
    )


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
    await _reload_tailwind_manager()
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
        cli_token=_cli_tailwind_token(),
        cache_path=cache_path,
    )
    env_active = source == "env" or source == "cli"
    reload_ok = False
    if not env_active:
        reload_ok = await _reload_tailwind_manager()
    return TailwindTokenSetOut(
        configured=bool(resolved),
        source=source,
        restart_required=not env_active and not reload_ok,
    )


def _cli_tailwind_token() -> str | None:
    args = runtime.cli_args
    if args is None:
        return None
    raw = getattr(args, "tailwind_token", None)
    return str(raw) if raw else None


def _kasa_settings_response(request: Request) -> KasaCredentialsSettingsOut:
    del request
    cache_path = runtime.discovery_cache_path()
    creds, source = resolve_kasa_credentials(cache_path=cache_path)
    stored = (
        kasa_credentials_stored_in_db(cache_path) if cache_path is not None else False
    )
    stored_username: str | None = None
    # Row existence (not decryptability) drives "password stored" UI state.
    password_stored = stored
    if cache_path is not None and stored:
        try:
            pair = load_kasa_credentials_from_db(cache_path)
        except SecretsDecryptError:
            pair = None
        if pair is not None:
            stored_username, _password = pair
    skipped: list[str] = []
    klap_hosts: list[str] = []
    state = runtime.device_state
    if state is not None:
        skipped = list(state.kasa_mgr.skipped_auth_hosts)
        klap_hosts = list(state.kasa_mgr.hosts_requiring_klap_auth)
    return KasaCredentialsSettingsOut(
        configured=creds is not None,
        source=source,
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        stored_in_database=stored,
        stored_username=stored_username if stored else None,
        password_stored=password_stored,
        skipped_auth_hosts=skipped,
        hosts_requiring_klap_auth=klap_hosts,
    )


async def _reload_kasa_manager() -> bool:
    """Apply resolved Kasa credentials on the live manager and rediscover.

    Returns ``False`` when discovery is not ready or hot-reload fails after
    credentials were already persisted (caller should set ``restart_required``).
    """
    state: DeviceManagersState | None = runtime.device_state
    if state is None:
        return False
    cache_path = runtime.discovery_cache_path()
    creds, _source = resolve_kasa_credentials(cache_path=cache_path)
    mgr = state.kasa_mgr
    if creds is None:
        mgr.clear_credentials()
    else:
        mgr.set_credentials(username=creds.username, password=creds.password)
    try:
        await mgr.rediscover()
        await runtime.restart_device_state_watchers()
    except Exception:
        return False
    return True


async def _reload_tailwind_manager() -> bool:
    """Re-bootstrap GoTailwind on the live server after token storage changes."""
    state: DeviceManagersState | None = runtime.device_state
    if state is None:
        return False
    cache_path = runtime.discovery_cache_path()
    token, _source = resolve_tailwind_token(
        cli_token=_cli_tailwind_token(),
        cache_path=cache_path,
    )
    if state.tailwind_mgr is not None:
        await state.tailwind_mgr.disconnect()
    if not token:
        runtime.device_state = state._replace(tailwind_mgr=None)
        return False
    mgr, _exc = await _bootstrap_tailwind(
        args=state.args,
        cache_path=cache_path,
        theme=_Theme(enabled=False),
        token=token,
        log_failures=True,
    )
    runtime.device_state = state._replace(tailwind_mgr=mgr)
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
        cli_token=_cli_tailwind_token(),
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
