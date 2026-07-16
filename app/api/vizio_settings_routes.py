"""HTTP routes for Vizio SmartCast pairing and credential status."""

from __future__ import annotations

import os
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import device_discovery_store
from app.api.schemas import (
    SettingsCredentialsTestOut,
    VizioAuthTestIn,
    VizioAuthTokenSetIn,
    VizioAuthTokenSetOut,
    VizioPairBeginIn,
    VizioPairBeginOut,
    VizioPairCancelIn,
    VizioPairCompleteIn,
    VizioPairCompleteOut,
    VizioTvSettingsOut,
    VizioTvsSettingsOut,
)
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    delete_app_secret,
    load_vizio_auth_hosts_from_db,
    load_vizio_auth_token_from_db,
    save_vizio_auth_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    vizio_auth_token_stored_in_db,
)
from app.domesti_bot_cli import DeviceManagersState
from app.server_runtime import runtime
from app.settings_credentials_test import (
    CredentialsTestUnavailableError,
    probe_vizio_auth,
)
from app.vizio_credentials import (
    resolve_vizio_auth_token,
    vizio_auth_secret_key_for_host,
    vizio_auth_secret_key_for_mac,
    vizio_device_id_from_parts,
)
from app.vizio_device_manager import VizioDeviceManager, configured_vizio_host_specs
from app.vizio_mac import (
    is_vizio_mac_device_id,
    lookup_ip_via_arp_for_mac,
)
from app.vizio_smartcast_client import (
    DEFAULT_VIZIO_PORT,
    VizioDeviceInfoSnapshot,
    VizioPairChallenge,
    VizioSmartCastBusyError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
    device_id_for,
    parse_host_spec,
    resolve_vizio_tv_mac,
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
    return VizioTvsSettingsOut(
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        tvs=_vizio_tv_settings_rows(cache_path),
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
    host, port, mac = _resolve_tv_endpoint(device_id, cache_path)
    if mac:
        delete_app_secret(cache_path, key=vizio_auth_secret_key_for_mac(mac))
    delete_app_secret(cache_path, key=vizio_auth_secret_key_for_host(host))
    _pending_pairing.pop(device_id_for(host, port), None)
    if mac:
        _pending_pairing.pop(vizio_device_id_from_parts(mac=mac, host=host, port=port), None)
    await _reload_vizio_manager()
    return _vizio_tv_settings_out(cache_path, host=host, port=port, mac=mac)


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
    host, port, _mac = _resolve_tv_endpoint(body.device_id, _optional_cache_path())
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
    host, port, _cached_mac = _resolve_tv_endpoint(body.device_id, cache_path)
    client = VizioSmartCastClient(host, port=port)
    challenge = VizioPairChallenge(
        challenge_type=body.challenge_type,
        pairing_req_token=body.pairing_req_token,
    )
    try:
        token = await client.pair_complete(challenge=challenge, pin=body.pin)
        info = await client.fetch_deviceinfo()
        mac = info.mac or await resolve_vizio_tv_mac(client, host=host)
    except VizioSmartCastConnectionError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    finally:
        await client.aclose()
    mac = _require_mac(mac, host=host)
    try:
        save_vizio_auth_token_to_db(cache_path, mac=mac, host=host, token=token)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    label = (info.cast_name or info.model_name or host).strip() or host
    device_discovery_store.upsert_vizio_tv(
        cache_path,
        host=host,
        port=port,
        display_name=label,
        model=info.model_name or None,
        mac=mac,
        diid=info.diid or None,
    )
    canonical_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
    _pending_pairing.pop(body.device_id, None)
    reload_ok = await _reload_vizio_manager()
    return VizioPairCompleteOut(
        configured=vizio_auth_token_stored_in_db(cache_path, mac=mac, host=host),
        device_id=canonical_id,
        restart_required=not reload_ok,
    )


@router.post(
    "/tvs/{device_id}/auth/test",
    response_model=SettingsCredentialsTestOut,
)
async def post_vizio_auth_test(
    device_id: str,
    body: VizioAuthTestIn,
    request: Request,
) -> SettingsCredentialsTestOut:
    """Probe SmartCast auth for one TV with an ephemeral client."""
    cache_path = discovery_cache_path_from_request(request)
    host, port, mac = _resolve_tv_endpoint(device_id, cache_path)
    try:
        result = await probe_vizio_auth(
            host=host,
            port=port,
            mac=mac,
            cache_path=cache_path,
            cli_token=_cli_vizio_token(),
            token=body.token,
        )
    except CredentialsTestUnavailableError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    return SettingsCredentialsTestOut(
        ok=result.ok,
        detail=result.detail,
        source=result.source,
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
    host, port, cached_mac = _resolve_tv_endpoint(device_id, cache_path)
    try:
        info = await _fetch_deviceinfo_optional(host, port, token=token)
        label = (info.cast_name or info.model_name or host).strip() if info else host
        mac: str | None = cached_mac
        if info is not None and info.mac is not None:
            mac = info.mac
        elif mac is None:
            client = VizioSmartCastClient(host, port=port, auth_token=token)
            try:
                mac = await resolve_vizio_tv_mac(client, host=host)
            finally:
                await client.aclose()
        mac = _require_mac(mac, host=host)
        save_vizio_auth_token_to_db(cache_path, mac=mac, host=host, token=token)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    device_discovery_store.upsert_vizio_tv(
        cache_path,
        host=host,
        port=port,
        display_name=label or None,
        model=info.model_name if info else None,
        mac=mac,
        diid=info.diid if info else None,
    )
    canonical_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
    reload_ok = await _reload_vizio_manager()
    return VizioAuthTokenSetOut(
        configured=vizio_auth_token_stored_in_db(cache_path, mac=mac, host=host),
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


def _display_name_for(
    cache_path: Path,
    *,
    host: str,
    port: int,
    mac: str | None,
) -> str | None:
    row = device_discovery_store.find_vizio_tv_row(
        cache_path,
        vizio_device_id_from_parts(mac=mac, host=host, port=port),
    )
    if row is None:
        return None
    return row[2]


def _require_mac(mac: str | None, *, host: str) -> str:
    if mac is None:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=(
                f"Could not resolve MAC address for TV at {host}. "
                "Ensure the TV is on the same LAN as this server and reachable."
            ),
        )
    return mac


def _resolve_tv_endpoint(
    device_id: str,
    cache_path: Path | None,
) -> tuple[str, int, str | None]:
    """Return ``(host, port, mac)`` for routes keyed by MAC or legacy host id."""
    needle = device_id.strip()
    if cache_path is not None:
        row = device_discovery_store.find_vizio_tv_row(cache_path, needle)
        if row is not None:
            host, port, _display, _model, mac, _diid = row
            return host, port, mac
    if is_vizio_mac_device_id(needle):
        ip = lookup_ip_via_arp_for_mac(needle)
        if ip is None:
            raise HTTPException(
                status_code=HTTPStatus.SERVICE_UNAVAILABLE,
                detail=(
                    f"Vizio TV {needle} is not reachable on the LAN. "
                    "Power on the TV or refresh the DHCP lease, then retry."
                ),
            )
        return ip, DEFAULT_VIZIO_PORT, needle
    try:
        host, port = parse_host_spec(needle)
        return host, port, None
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown Vizio device_id: {device_id}",
        ) from exc


def _optional_cache_path() -> Path | None:
    return runtime.discovery_cache_path()


def _stored_token_for_tv(
    cache_path: Path,
    *,
    host: str,
    mac: str | None,
) -> str | None:
    if not vizio_auth_token_stored_in_db(cache_path, mac=mac, host=host):
        return None
    try:
        return load_vizio_auth_token_from_db(cache_path, mac=mac, host=host)
    except SecretsDecryptError:
        return None


def _vizio_tv_settings_out(
    cache_path: Path,
    *,
    host: str,
    port: int,
    mac: str | None = None,
) -> VizioTvSettingsOut:
    canonical_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
    token, source = resolve_vizio_auth_token(
        mac=mac,
        host=host,
        cli_token=_cli_vizio_token(),
        env_token=os.environ.get("VIZIO_AUTH_TOKEN"),
        cache_path=cache_path,
    )
    stored = _stored_token_for_tv(cache_path, host=host, mac=mac) if source == "database" else None
    return VizioTvSettingsOut(
        device_id=canonical_id,
        mac=mac,
        host=host,
        port=port,
        display_name=_display_name_for(cache_path, host=host, port=port, mac=mac),
        auth_configured=bool(token),
        auth_source=source,
        stored_token=stored,
    )


def _vizio_tv_settings_rows(cache_path: Path) -> list[VizioTvSettingsOut]:
    seen: set[str] = set()
    rows: list[VizioTvSettingsOut] = []
    for host, port, _display, _model, mac, _diid in device_discovery_store.load_vizio_tvs(cache_path):
        canonical_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
        if canonical_id in seen:
            continue
        seen.add(canonical_id)
        rows.append(_vizio_tv_settings_out(cache_path, host=host, port=port, mac=mac))
    for auth_key in load_vizio_auth_hosts_from_db(cache_path):
        if is_vizio_mac_device_id(auth_key):
            row = device_discovery_store.find_vizio_tv_row(cache_path, auth_key)
            if row is not None:
                host, port, *_rest, mac, _diid = row
                canonical_id = vizio_device_id_from_parts(mac=mac, host=host, port=port)
                if canonical_id in seen:
                    continue
                seen.add(canonical_id)
                rows.append(_vizio_tv_settings_out(cache_path, host=host, port=port, mac=mac))
            continue
        try:
            parsed_host, port = parse_host_spec(auth_key)
        except ValueError:
            continue
        canonical_id = vizio_device_id_from_parts(mac=None, host=parsed_host, port=port)
        if canonical_id in seen:
            continue
        seen.add(canonical_id)
        rows.append(_vizio_tv_settings_out(cache_path, host=parsed_host, port=port, mac=None))
    rows.sort(key=lambda row: (row.display_name or row.device_id).lower())
    return rows


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
        force_discovery=False,
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
    await runtime.restart_device_state_watchers()
    return True
