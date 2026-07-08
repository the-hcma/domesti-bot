"""Flip UI device tiles via a single family-agnostic HTTP route."""

from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus

from fastapi import HTTPException

from app.api.schemas import UIDeviceOut
from app.api.ui_state import (
    build_kasa_device_view,
    build_sonos_device_view,
    build_tailwind_device_view,
    build_vizio_device_view,
    find_kasa_by_host,
    find_sonos_by_identifier,
    find_tailwind_by_identifier,
    find_vizio_by_id,
)
from app.device_enums import DeviceFamilyId
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.sonos_device_manager import SonosDeviceManager, SonosTransitionUnavailableError
from app.vizio_device_manager import VizioDeviceManager

_DEFAULT_SONOS_FAVORITE_INDEX = 0


@dataclass(frozen=True)
class UiDeviceFlipResult:
    """Outcome of flipping one UI device tile."""

    device: UIDeviceOut
    device_label: str
    log_detail: str


async def flip_ui_device(
    state: DeviceManagersState,
    *,
    family_id: str,
    device_id: str,
) -> UiDeviceFlipResult:
    """Read cached state, flip the device, return a refreshed tile view."""
    family = _parse_family_id(family_id)
    label = _device_label(state, family, device_id)
    try:
        log_detail = await _flip_device(state, family, device_id)
    except (KeyError, ValueError) as exc:
        lookup_error = _flip_lookup_error(exc, family, device_id)
        if lookup_error is not None:
            raise lookup_error from exc
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    except SonosTransitionUnavailableError as exc:
        raise HTTPException(
            status_code=HTTPStatus.CONFLICT,
            detail=str(exc),
        ) from exc
    return UiDeviceFlipResult(
        device=_build_device_view(state, family, device_id),
        device_label=label,
        log_detail=log_detail,
    )


def _build_device_view(
    state: DeviceManagersState,
    family: DeviceFamilyId,
    device_id: str,
) -> UIDeviceOut:
    match family:
        case DeviceFamilyId.KASA:
            return build_kasa_device_view(
                state.kasa_mgr, host=device_id, cache_path=state.cache_path
            )
        case DeviceFamilyId.SONOS:
            return build_sonos_device_view(
                _require_sonos_mgr(state, family),
                device_id=device_id,
                cache_path=state.cache_path,
            )
        case DeviceFamilyId.TAILWIND:
            return build_tailwind_device_view(
                _require_tailwind_mgr(state, family),
                device_id=device_id,
                cache_path=state.cache_path,
            )
        case DeviceFamilyId.VIZIO:
            return build_vizio_device_view(
                _require_vizio_mgr(state, family),
                device_id=device_id,
                cache_path=state.cache_path,
            )
        case DeviceFamilyId.ANDROIDTV:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"Unknown family_id: {family.value}",
            )


def _device_label(
    state: DeviceManagersState,
    family: DeviceFamilyId,
    device_id: str,
) -> str:
    match family:
        case DeviceFamilyId.KASA:
            kd = find_kasa_by_host(state.kasa_mgr, device_id)
            if kd is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"Unknown {DeviceFamilyId.KASA.display_name()} device: "
                        f"{device_id}"
                    ),
                )
            return kd.preferred_label
        case DeviceFamilyId.SONOS:
            if state.sonos_mgr is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"{DeviceFamilyId.SONOS.display_name()} manager is not "
                        "configured on this server"
                    ),
                )
            sp = find_sonos_by_identifier(state.sonos_mgr, device_id)
            if sp is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"Unknown {DeviceFamilyId.SONOS.display_name()} device: "
                        f"{device_id}"
                    ),
                )
            return sp.preferred_label
        case DeviceFamilyId.TAILWIND:
            if state.tailwind_mgr is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"{DeviceFamilyId.TAILWIND.display_name()} manager is not "
                        "configured on this server"
                    ),
                )
            gd = find_tailwind_by_identifier(state.tailwind_mgr, device_id)
            if gd is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"Unknown {DeviceFamilyId.TAILWIND.display_name()} device: "
                        f"{device_id}"
                    ),
                )
            return gd.preferred_label
        case DeviceFamilyId.VIZIO:
            if state.vizio_mgr is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"{DeviceFamilyId.VIZIO.display_name()} manager is not "
                        "configured on this server"
                    ),
                )
            tv = find_vizio_by_id(state.vizio_mgr, device_id)
            if tv is None:
                raise HTTPException(
                    status_code=HTTPStatus.NOT_FOUND,
                    detail=(
                        f"Unknown {DeviceFamilyId.VIZIO.display_name()} device: "
                        f"{device_id}"
                    ),
                )
            return tv.preferred_label
        case DeviceFamilyId.ANDROIDTV:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"Unknown family_id: {family.value}",
            )


async def _flip_device(
    state: DeviceManagersState,
    family: DeviceFamilyId,
    device_id: str,
) -> str:
    match family:
        case DeviceFamilyId.KASA:
            return await state.kasa_mgr.flip(device_id)
        case DeviceFamilyId.SONOS:
            return await _require_sonos_mgr(state, family).flip(
                device_id,
                favorite_index=_DEFAULT_SONOS_FAVORITE_INDEX,
            )
        case DeviceFamilyId.TAILWIND:
            return await _require_tailwind_mgr(state, family).flip(device_id)
        case DeviceFamilyId.VIZIO:
            return await _require_vizio_mgr(state, family).flip(device_id)
        case DeviceFamilyId.ANDROIDTV:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"Unknown family_id: {family.value}",
            )


def _flip_lookup_error(
    exc: BaseException,
    family: DeviceFamilyId,
    device_id: str,
) -> HTTPException | None:
    if isinstance(exc, KeyError):
        return HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown {family.display_name()} device: {device_id}",
        )
    if isinstance(exc, ValueError) and str(exc).startswith("Unknown"):
        return HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown {family.display_name()} device: {device_id}",
        )
    return None


def _manager_missing_detail(family: DeviceFamilyId) -> str:
    return f"{family.display_name()} manager is not configured on this server"


def _parse_family_id(family_id: str) -> DeviceFamilyId:
    try:
        return DeviceFamilyId(family_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Unknown family_id: {family_id}",
        ) from exc


def _require_sonos_mgr(
    state: DeviceManagersState,
    family: DeviceFamilyId,
) -> SonosDeviceManager:
    if state.sonos_mgr is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=_manager_missing_detail(family),
        )
    return state.sonos_mgr


def _require_tailwind_mgr(
    state: DeviceManagersState,
    family: DeviceFamilyId,
) -> GotailwindDeviceManager:
    if state.tailwind_mgr is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=_manager_missing_detail(family),
        )
    return state.tailwind_mgr


def _require_vizio_mgr(
    state: DeviceManagersState,
    family: DeviceFamilyId,
) -> VizioDeviceManager:
    if state.vizio_mgr is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=_manager_missing_detail(family),
        )
    return state.vizio_mgr
