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
from app.gotailwind_device_manager import GotailwindDevice
from app.sonos_device_manager import SonosTransitionUnavailableError
from app.vizio_device_manager import VizioTvDevice

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
    match family:
        case DeviceFamilyId.KASA:
            return await _flip_kasa(state, device_id=device_id)
        case DeviceFamilyId.SONOS:
            return await _flip_sonos(state, device_id=device_id)
        case DeviceFamilyId.TAILWIND:
            return await _flip_tailwind(state, device_id=device_id)
        case DeviceFamilyId.VIZIO:
            return await _flip_vizio(state, device_id=device_id)
        case DeviceFamilyId.ANDROIDTV:
            raise HTTPException(
                status_code=HTTPStatus.BAD_REQUEST,
                detail=f"Unknown family_id: {family_id}",
            )


def _parse_family_id(family_id: str) -> DeviceFamilyId:
    try:
        return DeviceFamilyId(family_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.BAD_REQUEST,
            detail=f"Unknown family_id: {family_id}",
        ) from exc


async def _flip_kasa(
    state: DeviceManagersState,
    *,
    device_id: str,
) -> UiDeviceFlipResult:
    kd = find_kasa_by_host(state.kasa_mgr, device_id)
    if kd is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=(
                f"Unknown {DeviceFamilyId.KASA.display_name()} device: {device_id}"
            ),
        )
    turn_on, log_detail = _switch_flip_target(kd.is_on)
    if turn_on:
        await kd.turn_on()
    else:
        await kd.turn_off()
    return UiDeviceFlipResult(
        device=build_kasa_device_view(
            state.kasa_mgr, host=device_id, cache_path=state.cache_path
        ),
        device_label=kd.preferred_label,
        log_detail=log_detail,
    )


async def _flip_sonos(
    state: DeviceManagersState,
    *,
    device_id: str,
) -> UiDeviceFlipResult:
    if state.sonos_mgr is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=(
                f"{DeviceFamilyId.SONOS.display_name()} manager is not configured "
                "on this server"
            ),
        )
    sp = find_sonos_by_identifier(state.sonos_mgr, device_id)
    if sp is None:
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=(
                f"Unknown {DeviceFamilyId.SONOS.display_name()} device: {device_id}"
            ),
        )
    resume, log_detail = _speaker_flip_target(sp.is_playing)
    try:
        if resume:
            await sp.resume(favorite_index=_DEFAULT_SONOS_FAVORITE_INDEX)
        else:
            await sp.pause()
    except ValueError as exc:
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
        device=build_sonos_device_view(
            state.sonos_mgr,
            device_id=device_id,
            cache_path=state.cache_path,
        ),
        device_label=sp.preferred_label,
        log_detail=log_detail,
    )


async def _flip_tailwind(
    state: DeviceManagersState,
    *,
    device_id: str,
) -> UiDeviceFlipResult:
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
    open_door, log_detail = _door_flip_target(gd)
    if open_door:
        await gd.open()
    else:
        await gd.close()
    return UiDeviceFlipResult(
        device=build_tailwind_device_view(
            state.tailwind_mgr,
            device_id=device_id,
            cache_path=state.cache_path,
        ),
        device_label=gd.preferred_label,
        log_detail=log_detail,
    )


async def _flip_vizio(
    state: DeviceManagersState,
    *,
    device_id: str,
) -> UiDeviceFlipResult:
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
                f"Unknown {DeviceFamilyId.VIZIO.display_name()} device: {device_id}"
            ),
        )
    turn_on, log_detail = _vizio_flip_target(tv)
    if turn_on:
        await tv.turn_on()
    else:
        await tv.turn_off()
    return UiDeviceFlipResult(
        device=build_vizio_device_view(
            state.vizio_mgr,
            device_id=device_id,
            cache_path=state.cache_path,
        ),
        device_label=tv.preferred_label,
        log_detail=log_detail,
    )


def _door_flip_target(door: GotailwindDevice) -> tuple[bool, str]:
    # Matches the web UI: only a fully closed door opens; open or
    # transient (unknown) positions close.
    if door.is_closed and not door.is_open:
        return True, "state=open"
    return False, "state=closed"


def _speaker_flip_target(is_playing: bool | None) -> tuple[bool, str]:
    # Matches ``nextStateAfterTileToggle`` for speakers: playing → pause;
    # paused or unknown → resume.
    if is_playing is True:
        return False, "playing=False"
    return True, "playing=True"


def _switch_flip_target(is_on: bool) -> tuple[bool, str]:
    # Matches ``nextStateAfterTileToggle`` for switches: on → off; off or
    # unknown → on.
    if is_on:
        return False, "on=False"
    return True, "on=True"


def _vizio_flip_target(tv: VizioTvDevice) -> tuple[bool, str]:
    state = tv.ui_power_state()
    if state == "on":
        return False, "on=False"
    if state == "off":
        return True, "on=True"
    return False, "on=False"
