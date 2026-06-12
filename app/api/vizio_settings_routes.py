"""HTTP routes for Vizio SmartCast pairing and credential status."""

from __future__ import annotations

import os
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import kasa_discovery_store
from app.api.schemas import (
    VizioAuthTokenSetIn,
    VizioAuthTokenSetOut,
    VizioPairBeginIn,
    VizioPairBeginOut,
    VizioPairCancelIn,
    VizioPairCompleteIn,
    VizioPairCompleteOut,
    VizioTvsSettingsOut,
    VizioTvSettingsOut,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import (
    SecretsConfigurationError,
    delete_app_secret,
    save_vizio_auth_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    vizio_auth_token_stored_in_db,
)
from app.domesti_bot_cli import DeviceManagersState
from app.server_runtime import runtime
from app.vizio_credentials import resolve_vizio_auth_token, vizio_auth_secret_key
from app.vizio_device_manager import VizioDeviceManager, configured_vizio_host_specs
from app.vizio_smartcast_client import (
    VizioDeviceInfoSnapshot,
    VizioPairChallenge,
    VizioSmartCastBusyError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
    device_id_for,
    parse_host_spec,
)

router = APIRouter(prefix="/v1/settings/vizio", tags=["settings"])

_pending_pairing: dict[str, VizioPairChallenge] = {}


@router.get("/tvs", response_model=VizioTvsSettingsOut)
async def list_vizio_tvs(request: Request) -> VizioTvsSettingsOut:
    """List cached Vizio TVs and whether each has an auth token."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return VizioTvsSettingsOut(
            secrets_key_configured=secrets_key_configured(),
            secrets_key_source=secrets_key_source(),
            tvs=[],
        )
    args = runtime.cli_args
    cli_token = getattr(args, "vizio_auth_token", None) if args is not None else None
    env_token = os.environ.get("VIZIO_AUTH_TOKEN")
    rows: list[VizioTvSettingsOut] = []
    for host, port, display, _model, _mac, _diid in kasa_discovery_store.load_vizio_tvs(
        cache_path
    ):
        device_id = device_id_for(host, port)
        token, source = resolve_vizio_auth_token(
            host=host,
            cli_token=cli_token,
            env_token=env_token,
            cache_path=cache_path,
        )
        rows.append(
            VizioTvSettingsOut(
                device_id=device_id,
                host=host,
                port=port,
                display_name=display,
                auth_configured=bool(token),
                auth_source=source,
            )
        )
    return VizioTvsSettingsOut(
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        tvs=rows,
    )


@router.delete("/auth/{device_id}", response_model=VizioTvSettingsOut)
async def clear_vizio_auth(device_id: str, request: Request) -> VizioTvSettingsOut:
    """Remove the encrypted per-TV auth token (CLI/env tokens are unchanged)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot clear stored Vizio auth: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    host, port = _host_port_for_device_id(device_id, cache_path)
    canonical_id = device_id_for(host, port)
    delete_app_secret(cache_path, key=vizio_auth_secret_key(host))
    _pending_pairing.pop(canonical_id, None)
    await _reload_vizio_manager()
    token, source = resolve_vizio_auth_token(
        host=host,
        cli_token=_cli_vizio_token(),
        env_token=os.environ.get("VIZIO_AUTH_TOKEN"),
        cache_path=cache_path,
    )
    display = _display_name_for(cache_path, host, port)
    return VizioTvSettingsOut(
        device_id=canonical_id,
        host=host,
        port=port,
        display_name=display,
        auth_configured=bool(token),
        auth_source=source,
    )


@router.post("/pair/begin", response_model=VizioPairBeginOut)
async def begin_vizio_pairing(body: VizioPairBeginIn) -> VizioPairBeginOut:
    """Start SmartCast pairing — a PIN appears on the TV."""
    host, port = parse_host_spec(body.host)
    device_id = device_id_for(host, port)
    client = VizioSmartCastClient(host, port=port)
    try:
        challenge = await _pair_begin_with_busy_retry(client, device_id)
    except VizioSmartCastConnectionError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    finally:
        await client.aclose()
    _pending_pairing[device_id] = challenge
    return VizioPairBeginOut(
        device_id=device_id,
        challenge_type=challenge.challenge_type,
        pairing_req_token=challenge.pairing_req_token,
    )


@router.post("/pair/cancel")
async def cancel_vizio_pairing(body: VizioPairCancelIn) -> dict[str, bool]:
    """Cancel an in-progress SmartCast pairing session."""
    host, port = _host_port_for_device_id(body.device_id, _optional_cache_path())
    client = VizioSmartCastClient(host, port=port)
    challenge = VizioPairChallenge(
        challenge_type=body.challenge_type,
        pairing_req_token=body.pairing_req_token,
    )
    try:
        await client.pair_cancel(challenge=challenge)
    except VizioSmartCastConnectionError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    finally:
        await client.aclose()
    _pending_pairing.pop(body.device_id, None)
    return {"ok": True}


@router.post("/pair/complete", response_model=VizioPairCompleteOut)
async def complete_vizio_pairing(
    body: VizioPairCompleteIn,
    request: Request,
) -> VizioPairCompleteOut:
    """Finish pairing with the on-screen PIN and store the auth token."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Vizio auth: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    host, port = _host_port_for_device_id(body.device_id, cache_path)
    client = VizioSmartCastClient(host, port=port)
    challenge = VizioPairChallenge(
        challenge_type=body.challenge_type,
        pairing_req_token=body.pairing_req_token,
    )
    try:
        token = await client.pair_complete(challenge=challenge, pin=body.pin)
        info = await client.fetch_deviceinfo()
    except VizioSmartCastConnectionError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    finally:
        await client.aclose()
    try:
        save_vizio_auth_token_to_db(cache_path, host=host, token=token)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    label = (info.cast_name or info.model_name or host).strip() or host
    kasa_discovery_store.upsert_vizio_tv(
        cache_path,
        host=host,
        port=port,
        display_name=label,
        model=info.model_name or None,
        mac=None,
        diid=info.diid or None,
    )
    _pending_pairing.pop(body.device_id, None)
    reload_ok = await _reload_vizio_manager()
    return VizioPairCompleteOut(
        configured=vizio_auth_token_stored_in_db(cache_path, host=host),
        device_id=body.device_id,
        restart_required=not reload_ok,
    )


@router.put("/tvs/{device_id}/auth", response_model=VizioAuthTokenSetOut)
async def put_vizio_auth_token(
    device_id: str,
    body: VizioAuthTokenSetIn,
    request: Request,
) -> VizioAuthTokenSetOut:
    """Encrypt and store a SmartCast auth token for one TV (no PIN re-pairing)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist Vizio auth: server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    token = body.token.strip()
    if not token:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected a non-empty token, got whitespace only",
        )
    host, port = _host_port_for_device_id(device_id, cache_path)
    canonical_id = device_id_for(host, port)
    try:
        save_vizio_auth_token_to_db(cache_path, host=host, token=token)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    info = await _fetch_deviceinfo_optional(host, port, token=token)
    label = (info.cast_name or info.model_name or host).strip() if info else host
    kasa_discovery_store.upsert_vizio_tv(
        cache_path,
        host=host,
        port=port,
        display_name=label or None,
        model=info.model_name if info else None,
        mac=None,
        diid=info.diid if info else None,
    )
    reload_ok = await _reload_vizio_manager()
    return VizioAuthTokenSetOut(
        configured=vizio_auth_token_stored_in_db(cache_path, host=host),
        device_id=canonical_id,
        restart_required=not reload_ok,
    )


async def _pair_begin_with_busy_retry(
    client: VizioSmartCastClient,
    device_id: str,
) -> VizioPairChallenge:
    pending = _pending_pairing.get(device_id)
    if pending is not None:
        try:
            await client.pair_cancel(challenge=pending)
        except VizioSmartCastConnectionError:
            pass
    try:
        return await client.pair_begin()
    except VizioSmartCastBusyError:
        if pending is None:
            raise HTTPException(
                status_code=HTTPStatus.CONFLICT,
                detail=(
                    "TV reports pairing already in progress. Wait for the "
                    "current PIN prompt to expire, or call pair/cancel with "
                    "the challenge from the previous begin response."
                ),
            ) from None
        await client.pair_cancel(challenge=pending)
        return await client.pair_begin()


def _cli_vizio_token() -> str | None:
    args = runtime.cli_args
    if args is None:
        return None
    raw = getattr(args, "vizio_auth_token", None)
    return str(raw) if raw else None


async def _fetch_deviceinfo_optional(
    host: str,
    port: int,
    *,
    token: str,
) -> VizioDeviceInfoSnapshot | None:
    client = VizioSmartCastClient(host, port=port, auth_token=token)
    try:
        return await client.fetch_deviceinfo()
    except VizioSmartCastConnectionError:
        return None
    finally:
        await client.aclose()


def _display_name_for(cache_path: Path, host: str, port: int) -> str | None:
    for row_host, row_port, display, *_rest in kasa_discovery_store.load_vizio_tvs(
        cache_path
    ):
        if row_host == host and row_port == port:
            return display
    return None


def _host_port_for_device_id(device_id: str, cache_path: Path | None) -> tuple[str, int]:
    needle = device_id.strip()
    if cache_path is not None:
        for host, port, *_rest in kasa_discovery_store.load_vizio_tvs(cache_path):
            if device_id_for(host, port) == needle:
                return host, port
    try:
        return parse_host_spec(needle)
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown Vizio device_id: {device_id}",
        ) from exc


def _optional_cache_path() -> Path | None:
    return runtime.discovery_cache_path()


async def _reload_vizio_manager() -> bool:
    """Re-bootstrap Vizio on the live server after credential changes."""
    state: DeviceManagersState | None = runtime.device_state
    if state is None:
        return False
    cache_path = runtime.discovery_cache_path()
    args = state.args
    if getattr(args, "no_vizio", False):
        if state.vizio_mgr is not None:
            await state.vizio_mgr.disconnect()
        runtime.device_state = state._replace(vizio_mgr=None)
        return False
    configured = configured_vizio_host_specs(
        cli_hosts=list(getattr(args, "vizio_host", None) or []),
        env_hosts=os.environ.get("VIZIO_HOSTS"),
    )
    env_token = os.environ.get("VIZIO_AUTH_TOKEN")
    if state.vizio_mgr is not None:
        await state.vizio_mgr.disconnect()
    mgr = VizioDeviceManager(
        configured_hosts=configured,
        discovery_cache_path=cache_path,
        cli_auth_token=getattr(args, "vizio_auth_token", None),
        env_auth_token=env_token,
        force_discovery=True,
    )
    try:
        await mgr.fetch()
    except Exception:
        await mgr.disconnect()
        runtime.device_state = state._replace(vizio_mgr=None)
        return False
    if not mgr.tvs:
        await mgr.disconnect()
        runtime.device_state = state._replace(vizio_mgr=None)
        return False
    runtime.device_state = state._replace(vizio_mgr=mgr)
    return True
