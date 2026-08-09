"""HTTP routes for operator settings (encrypted secrets, no device state required)."""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import device_discovery_store
from app.api.schemas import (
    Ep1BleAdvertisementSampleOut,
    Ep1BluetoothProxyOut,
    Ep1BluetoothProxySetIn,
    Ep1BluetoothProxyTestIn,
    Ep1BluetoothProxyTestOut,
    Ep1CalibrationOffsetFieldOut,
    Ep1CalibrationOut,
    Ep1CalibrationSetIn,
    Ep1DeviceSettingsOut,
    Ep1DevicesSettingsOut,
    Ep1NoisePreSharedKeySetIn,
    Ep1NoisePreSharedKeySetOut,
    Ep1NoisePreSharedKeySettingsOut,
    Ep1NoisePreSharedKeyTestIn,
    Ep1OccupancyTuningFieldOut,
    Ep1OccupancyTuningOut,
    Ep1OccupancyTuningSetIn,
    KasaCredentialsSetIn,
    KasaCredentialsSetOut,
    KasaCredentialsSettingsOut,
    KasaCredentialsTestIn,
    KasaDeviceSettingsOut,
    KasaDevicesSettingsOut,
    KasaMotionTuningOut,
    KasaMotionTuningSetIn,
    SettingsCredentialsTestOut,
    TailwindTokenSetIn,
    TailwindTokenSetOut,
    TailwindTokenSettingsOut,
    TailwindTokenTestIn,
)
from app.db.secrets import (
    SecretsConfigurationError,
    SecretsDecryptError,
    delete_app_secret,
    delete_kasa_credentials_from_db,
    ep1_noise_psk_stored_in_db,
    kasa_credentials_stored_in_db,
    load_ep1_noise_psk_from_db,
    load_kasa_credentials_from_db,
    load_tailwind_token_from_db,
    save_ep1_noise_psk_to_db,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    secrets_key_configured,
    secrets_key_source,
    tailwind_token_stored_in_db,
)
from app.device_enums import Ep1CalibrationOffsetKind, Ep1OccupancyTuningKind
from app.domesti_bot_cli import DeviceManagersState, _bootstrap_tailwind, _parse_ep1_host_specs, _Theme
from app.ep1_bluetooth_proxy import (
    DEFAULT_BLE_LISTEN_DURATION_S,
    Ep1BleAdvertisementSample,
    Ep1BluetoothProxyError,
    Ep1BluetoothProxyNotFoundError,
    Ep1BluetoothProxySnapshot,
    Ep1BluetoothProxyTestResult,
    Ep1BluetoothProxyValidationError,
    probe_ep1_bluetooth_proxy,
    read_ep1_bluetooth_proxy,
    set_ep1_bluetooth_proxy,
)
from app.ep1_calibration import (
    Ep1CalibrationError,
    Ep1CalibrationNotFoundError,
    Ep1CalibrationOffsetField,
    Ep1CalibrationSnapshot,
    Ep1CalibrationValidationError,
    apply_ep1_calibration_offsets,
    list_ep1_settings_targets,
    read_ep1_calibration,
)
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import DEFAULT_EP1_ZEROCONF_TIMEOUT_S, Ep1DeviceManager
from app.ep1_occupancy_tuning import (
    Ep1OccupancyTuningError,
    Ep1OccupancyTuningField,
    Ep1OccupancyTuningNotFoundError,
    Ep1OccupancyTuningSnapshot,
    Ep1OccupancyTuningValidationError,
    apply_ep1_occupancy_tuning,
    read_ep1_occupancy_tuning,
)
from app.kasa_credentials import resolve_kasa_credentials
from app.kasa_device_manager import KasaDeviceManager
from app.kasa_motion_tuning import (
    KasaMotionTuningError,
    KasaMotionTuningNotFoundError,
    KasaMotionTuningSnapshot,
    KasaMotionTuningValidationError,
    apply_kasa_motion_tuning,
    list_kasa_motion_settings_targets,
    read_kasa_motion_tuning,
)
from app.server_runtime import runtime
from app.settings_credentials_test import (
    CredentialsTestUnavailableError,
    probe_ep1_noise_psk,
    probe_kasa_credentials,
    probe_tailwind_token,
)
from app.tailwind_credentials import resolve_tailwind_token

router = APIRouter(prefix="/v1/settings", tags=["settings"])
_LOGGER = logging.getLogger(__name__)


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
    """Return Kasa credential status (stored password returned when in database)."""
    return _kasa_settings_response(request)


@router.post("/kasa-credentials/test", response_model=SettingsCredentialsTestOut)
async def post_kasa_credentials_test(
    body: KasaCredentialsTestIn,
    request: Request,
) -> SettingsCredentialsTestOut:
    """Probe KLAP auth on known hosts without touching the live manager."""
    del request
    cache_path = runtime.discovery_cache_path()
    try:
        result = await probe_kasa_credentials(
            cache_path=cache_path,
            username=body.username,
            password=body.password,
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


@router.put("/kasa-credentials", response_model=KasaCredentialsSetOut)
async def put_kasa_credentials(body: KasaCredentialsSetIn, request: Request) -> KasaCredentialsSetOut:
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


@router.get("/kasa/devices", response_model=KasaDevicesSettingsOut)
async def get_kasa_motion_devices_settings(request: Request) -> KasaDevicesSettingsOut:
    """List live Kasa switches that expose PIR (motion) for Settings → Target device."""
    del request
    devices = [
        KasaDeviceSettingsOut(
            device_id=row.device_id,
            display_label=row.display_label,
            display_name=row.display_name,
            host=row.host,
            model=row.model,
        )
        for row in list_kasa_motion_settings_targets(kasa_mgr=_live_kasa_mgr())
    ]
    return KasaDevicesSettingsOut(devices=devices)


@router.get(
    "/kasa/devices/{device_id}/motion-tuning",
    response_model=KasaMotionTuningOut,
)
async def get_kasa_device_motion_tuning(device_id: str, request: Request) -> KasaMotionTuningOut:
    """Read PIR / ambient config and live sensors for one motion-capable Kasa switch."""
    del request
    try:
        snapshot = await read_kasa_motion_tuning(
            device_id=device_id,
            kasa_mgr=_live_kasa_mgr(),
        )
    except KasaMotionTuningNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except KasaMotionTuningError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _kasa_motion_tuning_out(snapshot)


@router.put(
    "/kasa/devices/{device_id}/motion-tuning",
    response_model=KasaMotionTuningOut,
)
async def put_kasa_device_motion_tuning(
    device_id: str,
    body: KasaMotionTuningSetIn,
    request: Request,
) -> KasaMotionTuningOut:
    """Write PIR / ambient config knobs on the target Kasa switch."""
    del request
    try:
        snapshot = await apply_kasa_motion_tuning(
            device_id=device_id,
            ambient_light_enabled=body.ambient_light_enabled,
            inactivity_timeout_ms=body.inactivity_timeout_ms,
            kasa_mgr=_live_kasa_mgr(),
            pir_enabled=body.pir_enabled,
            pir_range=body.pir_range,
            pir_threshold=body.pir_threshold,
        )
    except KasaMotionTuningNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except KasaMotionTuningValidationError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except KasaMotionTuningError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _kasa_motion_tuning_out(snapshot)


@router.get("/ep1/devices", response_model=Ep1DevicesSettingsOut)
async def get_ep1_devices_settings(request: Request) -> Ep1DevicesSettingsOut:
    """List known EP1 sensors for Settings → Target device."""
    del request
    cache_path = runtime.discovery_cache_path()
    state = runtime.device_state
    ep1_mgr = state.ep1_mgr if state is not None else None
    devices = [
        Ep1DeviceSettingsOut(
            device_id=row.device_id,
            display_label=row.display_label,
            display_name=row.display_name,
            host=row.host,
            port=row.port,
        )
        for row in list_ep1_settings_targets(cache_path=cache_path, ep1_mgr=ep1_mgr)
    ]
    return Ep1DevicesSettingsOut(devices=devices)


@router.get(
    "/ep1/devices/{device_id}/bluetooth-proxy",
    response_model=Ep1BluetoothProxyOut,
)
async def get_ep1_device_bluetooth_proxy(device_id: str, request: Request) -> Ep1BluetoothProxyOut:
    """Read the ``bluetooth_proxy`` select state for one EP1."""
    del request
    try:
        snapshot = await read_ep1_bluetooth_proxy(
            device_id=device_id,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1BluetoothProxyNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1BluetoothProxyError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_bluetooth_proxy_out(snapshot)


@router.put(
    "/ep1/devices/{device_id}/bluetooth-proxy",
    response_model=Ep1BluetoothProxyOut,
)
async def put_ep1_device_bluetooth_proxy(
    device_id: str,
    body: Ep1BluetoothProxySetIn,
    request: Request,
) -> Ep1BluetoothProxyOut:
    """Enable or disable the EP1 ``bluetooth_proxy`` select entity."""
    del request
    try:
        snapshot = await set_ep1_bluetooth_proxy(
            device_id=device_id,
            enabled=body.enabled,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1BluetoothProxyNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1BluetoothProxyValidationError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Ep1BluetoothProxyError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_bluetooth_proxy_out(snapshot)


@router.post(
    "/ep1/devices/{device_id}/bluetooth-proxy/test",
    response_model=Ep1BluetoothProxyTestOut,
)
async def post_ep1_device_bluetooth_proxy_test(
    device_id: str,
    body: Ep1BluetoothProxyTestIn,
    request: Request,
) -> Ep1BluetoothProxyTestOut:
    """Enable ``bluetooth_proxy`` if needed and listen for BLE advertisements."""
    del request
    duration_s = body.duration_s if body.duration_s is not None else DEFAULT_BLE_LISTEN_DURATION_S
    try:
        result = await probe_ep1_bluetooth_proxy(
            device_id=device_id,
            duration_s=duration_s,
            enable_if_needed=body.enable_if_needed,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1BluetoothProxyNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1BluetoothProxyValidationError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Ep1BluetoothProxyError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_bluetooth_proxy_test_out(result)


@router.get(
    "/ep1/devices/{device_id}/calibration",
    response_model=Ep1CalibrationOut,
)
async def get_ep1_device_calibration(device_id: str, request: Request) -> Ep1CalibrationOut:
    """Read humidity / illuminance / temperature offsets for one EP1."""
    del request
    try:
        snapshot = await read_ep1_calibration(
            device_id=device_id,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1CalibrationNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1CalibrationError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_calibration_out(snapshot)


@router.put(
    "/ep1/devices/{device_id}/calibration",
    response_model=Ep1CalibrationOut,
)
async def put_ep1_device_calibration(
    device_id: str,
    body: Ep1CalibrationSetIn,
    request: Request,
) -> Ep1CalibrationOut:
    """Write one or more climate / light calibration offsets on the target EP1."""
    del request
    try:
        snapshot = await apply_ep1_calibration_offsets(
            device_id=device_id,
            humidity_offset=body.humidity_offset,
            illuminance_offset=body.illuminance_offset,
            temperature_offset=body.temperature_offset,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1CalibrationNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1CalibrationValidationError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Ep1CalibrationError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_calibration_out(snapshot)


@router.get(
    "/ep1/devices/{device_id}/occupancy-tuning",
    response_model=Ep1OccupancyTuningOut,
)
async def get_ep1_device_occupancy_tuning(device_id: str, request: Request) -> Ep1OccupancyTuningOut:
    """Read mmWave distance / sensitivity / latency knobs for one EP1."""
    del request
    try:
        snapshot = await read_ep1_occupancy_tuning(
            device_id=device_id,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1OccupancyTuningNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1OccupancyTuningError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_occupancy_tuning_out(snapshot)


@router.put(
    "/ep1/devices/{device_id}/occupancy-tuning",
    response_model=Ep1OccupancyTuningOut,
)
async def put_ep1_device_occupancy_tuning(
    device_id: str,
    body: Ep1OccupancyTuningSetIn,
    request: Request,
) -> Ep1OccupancyTuningOut:
    """Write mmWave occupancy tuning knobs on the target EP1 (auto Set Distance / Sensitivity)."""
    del request
    try:
        snapshot = await apply_ep1_occupancy_tuning(
            device_id=device_id,
            max_distance=body.max_distance,
            min_distance=body.min_distance,
            off_latency=body.off_latency,
            on_latency=body.on_latency,
            sustain_sensitivity=body.sustain_sensitivity,
            trigger_distance=body.trigger_distance,
            trigger_sensitivity=body.trigger_sensitivity,
            cache_path=runtime.discovery_cache_path(),
            cli_noise_psk=_cli_ep1_noise_psk(),
            ep1_mgr=_live_ep1_mgr(),
        )
    except Ep1OccupancyTuningNotFoundError as exc:
        raise HTTPException(status_code=HTTPStatus.NOT_FOUND, detail=str(exc)) from exc
    except Ep1OccupancyTuningValidationError as exc:
        raise HTTPException(status_code=HTTPStatus.UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    except Ep1OccupancyTuningError as exc:
        raise HTTPException(status_code=HTTPStatus.BAD_GATEWAY, detail=str(exc)) from exc
    return _ep1_occupancy_tuning_out(snapshot)


@router.delete("/ep1-noise-psk", response_model=Ep1NoisePreSharedKeySettingsOut)
async def clear_ep1_noise_psk(request: Request) -> Ep1NoisePreSharedKeySettingsOut:
    """Remove the encrypted database Noise pre-shared key (PSK).

    Environment / CLI values are unchanged.
    """
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot clear stored EP1 Noise pre-shared key (PSK): server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    delete_app_secret(cache_path, key="ep1_noise_psk")
    await _reload_ep1_manager()
    return _ep1_settings_response(request)


@router.get("/ep1-noise-psk", response_model=Ep1NoisePreSharedKeySettingsOut)
async def get_ep1_noise_psk_settings(request: Request) -> Ep1NoisePreSharedKeySettingsOut:
    """Return EP1 Noise pre-shared key (PSK) status (includes stored DB value when present)."""
    return _ep1_settings_response(request)


@router.post("/ep1-noise-psk/test", response_model=SettingsCredentialsTestOut)
async def post_ep1_noise_psk_test(
    body: Ep1NoisePreSharedKeyTestIn,
    request: Request,
) -> SettingsCredentialsTestOut:
    """Probe the ESPHome Noise pre-shared key (PSK) with an ephemeral client."""
    cache_path = discovery_cache_path_from_request(request)
    try:
        result = await probe_ep1_noise_psk(
            cache_path=cache_path,
            cli_psk=_cli_ep1_noise_psk(),
            psk=body.noise_psk,
            device_id=body.device_id,
            host=body.host,
            ep1_mgr=_live_ep1_mgr(),
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


@router.put("/ep1-noise-psk", response_model=Ep1NoisePreSharedKeySetOut)
async def put_ep1_noise_psk(
    body: Ep1NoisePreSharedKeySetIn,
    request: Request,
) -> Ep1NoisePreSharedKeySetOut:
    """Encrypt and store the EP1 ESPHome Noise pre-shared key (PSK)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=(
                "Cannot persist EP1 Noise pre-shared key (PSK): server started with "
                "--no-discovery-cache. Restart with a discovery cache path."
            ),
        )
    psk = body.noise_psk.strip()
    if not psk:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail="Expected a non-empty Noise pre-shared key (PSK), got whitespace only",
        )
    try:
        save_ep1_noise_psk_to_db(cache_path, psk)
    except SecretsConfigurationError as exc:
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    resolved, source = resolve_ep1_noise_psk(
        cli_psk=_cli_ep1_noise_psk(),
        cache_path=cache_path,
    )
    env_active = source in ("env", "cli")
    reload_ok = False
    if not env_active:
        reload_ok = await _reload_ep1_manager()
    return Ep1NoisePreSharedKeySetOut(
        configured=bool(resolved),
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


@router.post("/tailwind-token/test", response_model=SettingsCredentialsTestOut)
async def post_tailwind_token_test(
    body: TailwindTokenTestIn,
    request: Request,
) -> SettingsCredentialsTestOut:
    """Probe the Local Control Key with an ephemeral Tailwind client."""
    cache_path = discovery_cache_path_from_request(request)
    try:
        result = await probe_tailwind_token(
            cache_path=cache_path,
            cli_token=_cli_tailwind_token(),
            token=body.token,
            host=body.host,
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


@router.put("/tailwind-token", response_model=TailwindTokenSetOut)
async def put_tailwind_token(body: TailwindTokenSetIn, request: Request) -> TailwindTokenSetOut:
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


def _cli_ep1_noise_psk() -> str | None:
    args = runtime.cli_args
    if args is None:
        return None
    raw = getattr(args, "ep1_noise_psk", None)
    return str(raw) if raw else None


def _cli_tailwind_token() -> str | None:
    args = runtime.cli_args
    if args is None:
        return None
    raw = getattr(args, "tailwind_token", None)
    return str(raw) if raw else None


def _ep1_ble_advertisement_sample_out(sample: Ep1BleAdvertisementSample) -> Ep1BleAdvertisementSampleOut:
    return Ep1BleAdvertisementSampleOut(
        address=sample.address,
        address_type=sample.address_type,
        data_length=sample.data_length,
        known_test_beacon_label=sample.known_test_beacon_label,
        rssi=sample.rssi,
    )


def _ep1_bluetooth_proxy_out(snapshot: Ep1BluetoothProxySnapshot) -> Ep1BluetoothProxyOut:
    return Ep1BluetoothProxyOut(
        available=snapshot.available,
        device_id=snapshot.device_id,
        display_label=snapshot.display_label,
        display_name=snapshot.display_name,
        host=snapshot.host,
        options=list(snapshot.options),
        port=snapshot.port,
        state=snapshot.state,
    )


def _ep1_bluetooth_proxy_test_out(result: Ep1BluetoothProxyTestResult) -> Ep1BluetoothProxyTestOut:
    return Ep1BluetoothProxyTestOut(
        detail=result.detail,
        devices=[_ep1_ble_advertisement_sample_out(sample) for sample in result.samples],
        duration_s=result.duration_s,
        ok=result.ok,
        proxy_state=result.proxy_state,
        proxy_was_enabled=result.proxy_was_enabled,
    )


def _ep1_calibration_out(snapshot: Ep1CalibrationSnapshot) -> Ep1CalibrationOut:
    return Ep1CalibrationOut(
        device_id=snapshot.device_id,
        display_label=snapshot.display_label,
        display_name=snapshot.display_name,
        host=snapshot.host,
        humidity=_ep1_offset_field_out(snapshot.offsets[Ep1CalibrationOffsetKind.HUMIDITY]),
        illuminance=_ep1_offset_field_out(snapshot.offsets[Ep1CalibrationOffsetKind.ILLUMINANCE]),
        offsets_confirmed=snapshot.offsets_confirmed,
        port=snapshot.port,
        readings_refreshed=snapshot.readings_refreshed,
        temperature=_ep1_offset_field_out(snapshot.offsets[Ep1CalibrationOffsetKind.TEMPERATURE]),
    )


def _ep1_occupancy_tuning_field_out(field: Ep1OccupancyTuningField) -> Ep1OccupancyTuningFieldOut:
    return Ep1OccupancyTuningFieldOut(
        available=field.available,
        kind=field.kind,
        max_value=field.max_value,
        min_value=field.min_value,
        step=field.step,
        unit=field.unit,
        value=field.value,
    )


def _ep1_occupancy_tuning_out(snapshot: Ep1OccupancyTuningSnapshot) -> Ep1OccupancyTuningOut:
    return Ep1OccupancyTuningOut(
        device_id=snapshot.device_id,
        display_label=snapshot.display_label,
        display_name=snapshot.display_name,
        distance_applied=snapshot.distance_applied,
        host=snapshot.host,
        knobs_confirmed=snapshot.knobs_confirmed,
        max_distance=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.MAX_DISTANCE]),
        min_distance=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.MIN_DISTANCE]),
        off_latency=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.OFF_LATENCY]),
        on_latency=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.ON_LATENCY]),
        port=snapshot.port,
        sensitivity_applied=snapshot.sensitivity_applied,
        sustain_sensitivity=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY]),
        trigger_distance=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.TRIGGER_DISTANCE]),
        trigger_sensitivity=_ep1_occupancy_tuning_field_out(snapshot.knobs[Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY]),
    )


def _ep1_offset_field_out(field: Ep1CalibrationOffsetField) -> Ep1CalibrationOffsetFieldOut:
    return Ep1CalibrationOffsetFieldOut(
        available=field.available,
        kind=field.kind,
        max_value=field.max_value,
        min_value=field.min_value,
        reading=field.reading,
        step=field.step,
        unit=field.unit,
        value=field.value,
    )


def _ep1_settings_response(request: Request) -> Ep1NoisePreSharedKeySettingsOut:
    del request
    cache_path = runtime.discovery_cache_path()
    psk, source = resolve_ep1_noise_psk(
        cli_psk=_cli_ep1_noise_psk(),
        cache_path=cache_path,
    )
    stored = ep1_noise_psk_stored_in_db(cache_path) if cache_path is not None else False
    stored_psk: str | None = None
    if cache_path is not None and stored:
        try:
            stored_psk = load_ep1_noise_psk_from_db(cache_path)
        except SecretsDecryptError:
            stored_psk = None
    return Ep1NoisePreSharedKeySettingsOut(
        configured=bool(psk),
        source=source,
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        stored_in_database=stored,
        stored_noise_psk=stored_psk if stored and source not in ("env", "cli") else None,
    )


def _live_ep1_mgr() -> Ep1DeviceManager | None:
    state = runtime.device_state
    if state is None:
        return None
    return state.ep1_mgr


def _live_kasa_mgr() -> KasaDeviceManager | None:
    state = runtime.device_state
    if state is None:
        return None
    return state.kasa_mgr


async def _reload_ep1_manager() -> bool:
    """Rebuild the live EP1 manager after Noise PSK storage changes."""
    state: DeviceManagersState | None = runtime.device_state
    if state is None:
        return False
    cache_path = runtime.discovery_cache_path()
    psk, _source = resolve_ep1_noise_psk(
        cli_psk=_cli_ep1_noise_psk(),
        cache_path=cache_path,
    )
    if state.ep1_mgr is not None:
        try:
            await state.ep1_mgr.disconnect()
        except Exception:
            _LOGGER.warning("EP1 manager disconnect during Settings reload failed", exc_info=True)
    cached_rows = device_discovery_store.load_ep1_devices(cache_path) if cache_path is not None else []
    raw_hosts = getattr(state.args, "ep1_host", None) or []
    hosts = _parse_ep1_host_specs(list(raw_hosts))
    want_zeroconf = not bool(getattr(state.args, "no_ep1_zeroconf", False))
    if not cached_rows and not hosts and not want_zeroconf:
        runtime.device_state = state._replace(ep1_mgr=None)
        return False
    mgr = Ep1DeviceManager(
        configured_hosts=hosts,
        discovery_cache_path=cache_path,
        cli_noise_psk=_cli_ep1_noise_psk(),
        noise_psk=psk or None,
        force_discovery=bool(getattr(state.args, "force_discovery", False)),
        zeroconf_discovery=want_zeroconf,
        zeroconf_timeout=float(getattr(state.args, "ep1_zeroconf_timeout", DEFAULT_EP1_ZEROCONF_TIMEOUT_S)),
    )
    try:
        await mgr.fetch()
    except Exception:
        await mgr.disconnect()
        runtime.device_state = state._replace(ep1_mgr=None)
        return False
    runtime.device_state = state._replace(ep1_mgr=mgr)
    try:
        await runtime.restart_device_state_watchers()
    except Exception:
        return False
    return True


def _kasa_motion_tuning_out(snapshot: KasaMotionTuningSnapshot) -> KasaMotionTuningOut:
    return KasaMotionTuningOut(
        ambient_available=snapshot.ambient_available,
        ambient_light=snapshot.ambient_light,
        ambient_light_enabled=snapshot.ambient_light_enabled,
        device_id=snapshot.device_id,
        display_label=snapshot.display_label,
        display_name=snapshot.display_name,
        host=snapshot.host,
        inactivity_timeout_ms=snapshot.inactivity_timeout_ms,
        knobs_confirmed=snapshot.knobs_confirmed,
        model=snapshot.model,
        pir_enabled=snapshot.pir_enabled,
        pir_percent=snapshot.pir_percent,
        pir_range=snapshot.pir_range,
        pir_range_choices=list(snapshot.pir_range_choices),
        pir_threshold=snapshot.pir_threshold,
        pir_triggered=snapshot.pir_triggered,
    )


def _kasa_settings_response(request: Request) -> KasaCredentialsSettingsOut:
    del request
    cache_path = runtime.discovery_cache_path()
    creds, source = resolve_kasa_credentials(cache_path=cache_path)
    stored = kasa_credentials_stored_in_db(cache_path) if cache_path is not None else False
    stored_password: str | None = None
    stored_username: str | None = None
    # Row existence (not decryptability) drives "password stored" UI state.
    password_stored = stored
    if cache_path is not None and stored:
        try:
            pair = load_kasa_credentials_from_db(cache_path)
        except SecretsDecryptError:
            pair = None
        if pair is not None:
            stored_username, stored_password = pair
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
        stored_password=stored_password if stored and source != "env" else None,
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
    stored = tailwind_token_stored_in_db(cache_path) if cache_path is not None else False
    return TailwindTokenSettingsOut(
        configured=bool(token),
        source=source,
        secrets_key_configured=secrets_key_configured(),
        secrets_key_source=secrets_key_source(),
        stored_in_database=stored,
        stored_token=_stored_token_for_settings(cache_path) if stored else None,
    )
