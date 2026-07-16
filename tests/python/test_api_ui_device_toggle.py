"""Tests for ``POST /v1/ui/devices/{family_id}/{device_id}/toggle``."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.sonos_device_manager import (
    SonosDeviceManager,
    SonosTransitionUnavailableError,
)
from app.vizio_device_manager import VizioDeviceManager


class _FakeDoor:
    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_open = is_open
        self.is_closed = not is_open
        self.calls: list[str] = []

    async def close(self) -> None:
        self.calls.append("close")
        self.is_open = False
        self.is_closed = True

    async def open(self) -> None:
        self.calls.append("open")
        self.is_open = True
        self.is_closed = False

    async def flip(self) -> str:
        if self.is_closed and not self.is_open:
            await self.open()
            return "state=open"
        await self.close()
        return "state=closed"


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True

    async def flip(self) -> str:
        if self.is_on:
            await self.turn_off()
            return "on=False"
        await self.turn_on()
        return "on=True"


class _FakeSonosZone:
    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        is_playing: bool | None,
        raise_transition_unavailable_on: str | None = None,
    ) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_playing = is_playing
        self.stream_favorites: tuple = ()
        self.calls: list[str] = []
        self._raise_on = raise_transition_unavailable_on

    async def pause(self) -> None:
        self.calls.append("pause")
        if self._raise_on == "pause":
            self.is_playing = False
            raise SonosTransitionUnavailableError(f"{self.preferred_label!r} cannot pause")
        self.is_playing = False

    async def resume(self, *, favorite_index: int = 0) -> None:
        self.calls.append(f"resume:{favorite_index}")
        if self._raise_on == "resume":
            self.is_playing = False
            raise SonosTransitionUnavailableError(f"{self.preferred_label!r} cannot resume")
        self.is_playing = True

    async def flip(self, *, favorite_index: int = 0) -> str:
        if self.is_playing is True:
            await self.pause()
            return "playing=False"
        await self.resume(favorite_index=favorite_index)
        return "playing=True"


class _FakeVizioTv:
    def __init__(
        self,
        device_id: str,
        label: str,
        *,
        power_state: str,
    ) -> None:
        self.identifier = device_id
        self.preferred_label = label
        self._power_state = power_state
        self.calls: list[str] = []

    def ui_power_state(self) -> str:
        return self._power_state

    async def turn_off(self) -> None:
        self.calls.append("off")
        self._power_state = "off"

    async def turn_on(self) -> None:
        self.calls.append("on")
        self._power_state = "on"

    async def flip(self) -> str:
        state = self.ui_power_state()
        if state == "on":
            await self.turn_off()
            return "on=False"
        if state == "off":
            await self.turn_on()
            return "on=True"
        await self.turn_off()
        return "on=False"


def _client() -> tuple[TestClient, FastAPI]:
    app = create_app(argparse.Namespace())
    return TestClient(app), app


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    host_to_device = {device._kDevice.host: device for device in devices}

    async def flip(identifier: str) -> str:
        device = host_to_device.get(identifier)
        if device is None:
            raise ValueError(f"Unknown device: {identifier!r}")
        return await device.flip()

    async def flip_tile(identifier: str) -> tuple[str, str]:
        device = host_to_device.get(identifier)
        if device is None:
            raise KeyError(identifier)
        detail = await device.flip()
        return device.preferred_label, detail

    mgr.flip = flip
    mgr.flip_tile = flip_tile
    return cast(KasaDeviceManager, mgr)


def _sonos_mgr(zones: list[_FakeSonosZone]) -> SonosDeviceManager:
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = tuple(zones)
    id_to_zone = {zone.identifier: zone for zone in zones}

    async def flip(identifier: str, *, favorite_index: int = 0) -> str:
        zone = id_to_zone.get(identifier)
        if zone is None:
            raise ValueError(f"Unknown device: {identifier!r}")
        return await zone.flip(favorite_index=favorite_index)

    async def flip_tile(
        identifier: str,
        *,
        favorite_index: int = 0,
    ) -> tuple[str, str]:
        zone = id_to_zone.get(identifier)
        if zone is None:
            raise KeyError(identifier)
        detail = await zone.flip(favorite_index=favorite_index)
        return zone.preferred_label, detail

    mgr.flip = flip
    mgr.flip_tile = flip_tile
    return cast(SonosDeviceManager, mgr)


def _tailwind_mgr(doors: list[_FakeDoor]) -> GotailwindDeviceManager:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    id_to_door = {door.identifier: door for door in doors}

    async def flip(identifier: str) -> str:
        door = id_to_door.get(identifier)
        if door is None:
            raise ValueError(f"Unknown device: {identifier!r}")
        return await door.flip()

    async def flip_tile(identifier: str) -> tuple[str, str]:
        door = id_to_door.get(identifier)
        if door is None:
            raise KeyError(identifier)
        detail = await door.flip()
        return door.preferred_label, detail

    mgr.flip = flip
    mgr.flip_tile = flip_tile
    return cast(GotailwindDeviceManager, mgr)


def _vizio_mgr(tvs: list[_FakeVizioTv]) -> VizioDeviceManager:
    mgr = MagicMock(spec=VizioDeviceManager)
    mgr.tvs = tuple(tvs)
    id_to_tv = {tv.identifier: tv for tv in tvs}

    async def flip(identifier: str) -> str:
        tv = id_to_tv.get(identifier)
        if tv is None:
            raise KeyError(identifier)
        return await tv.flip()

    async def flip_tile(identifier: str) -> tuple[str, str]:
        tv = id_to_tv.get(identifier)
        if tv is None:
            raise KeyError(identifier)
        detail = await tv.flip()
        return tv.preferred_label, detail

    mgr.flip = flip
    mgr.flip_tile = flip_tile
    return cast(VizioDeviceManager, mgr)


def _state(
    *,
    kasa_devices: list[_FakeKasa] | None = None,
    sonos_zones: list[_FakeSonosZone] | None = None,
    tailwind_doors: list[_FakeDoor] | None = None,
    vizio_tvs: list[_FakeVizioTv] | None = None,
    cache_path: Path | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr(kasa_devices or []),
        sonos_mgr=_sonos_mgr(sonos_zones) if sonos_zones is not None else None,
        tailwind_mgr=_tailwind_mgr(tailwind_doors) if tailwind_doors else None,
        androidtv_mgr=None,
        vizio_mgr=_vizio_mgr(vizio_tvs) if vizio_tvs is not None else None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def test_post_ui_device_toggle_flips_kasa_switch_off() -> None:
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=True)
    client, _ = _client()
    runtime.device_state = _state(kasa_devices=[fake])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/kasa/10.0.0.1/toggle")

    assert response.status_code == HTTPStatus.OK
    assert fake.calls == ["off"]
    assert response.json()["device"]["state"] == "off"


def test_post_ui_device_toggle_flips_kasa_switch_on() -> None:
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=False)
    client, _ = _client()
    runtime.device_state = _state(kasa_devices=[fake])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/kasa/10.0.0.1/toggle")

    assert response.status_code == HTTPStatus.OK
    assert fake.calls == ["on"]
    assert response.json()["device"]["state"] == "on"


def test_post_ui_device_toggle_returns_404_for_unknown_kasa_device() -> None:
    client, _ = _client()
    runtime.device_state = _state(kasa_devices=[])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/kasa/10.0.0.99/toggle")

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_post_ui_device_toggle_returns_400_for_unknown_family() -> None:
    client, _ = _client()
    runtime.device_state = _state()
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/androidtv/cast-1/toggle")

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_post_ui_device_toggle_pauses_playing_sonos_zone() -> None:
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, _ = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/sonos/RINCON_A/toggle")

    assert response.status_code == HTTPStatus.OK
    assert zone.calls == ["pause"]
    assert response.json()["device"]["state"] == "paused"


def test_post_ui_device_toggle_resumes_paused_sonos_zone() -> None:
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=False)
    client, _ = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/sonos/RINCON_A/toggle")

    assert response.status_code == HTTPStatus.OK
    assert zone.calls == ["resume:0"]
    assert response.json()["device"]["state"] == "playing"


def test_post_ui_device_toggle_returns_409_for_sonos_transition_error() -> None:
    zone = _FakeSonosZone(
        "RINCON_A",
        "Kitchen",
        is_playing=False,
        raise_transition_unavailable_on="resume",
    )
    client, _ = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/sonos/RINCON_A/toggle")

    assert response.status_code == HTTPStatus.CONFLICT
    assert "cannot resume" in response.json()["detail"]


def test_post_ui_device_toggle_opens_closed_tailwind_door() -> None:
    door = _FakeDoor("door-1", "Left", is_open=False)
    client, _ = _client()
    runtime.device_state = _state(tailwind_doors=[door])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/tailwind/door-1/toggle")

    assert response.status_code == HTTPStatus.OK
    assert door.calls == ["open"]
    assert response.json()["device"]["state"] == "open"


def test_post_ui_device_toggle_closes_open_tailwind_door() -> None:
    door = _FakeDoor("door-1", "Left", is_open=True)
    client, _ = _client()
    runtime.device_state = _state(tailwind_doors=[door])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/tailwind/door-1/toggle")

    assert response.status_code == HTTPStatus.OK
    assert door.calls == ["close"]
    assert response.json()["device"]["state"] == "closed"


def test_post_ui_device_toggle_turns_off_on_vizio_tv() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Living room", power_state="on")
    client, _ = _client()
    runtime.device_state = _state(vizio_tvs=[tv])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/vizio/192.168.1.10/toggle")

    assert response.status_code == HTTPStatus.OK
    assert tv.calls == ["off"]
    assert response.json()["device"]["state"] == "off"


def test_post_ui_device_toggle_turns_off_unknown_vizio_tv() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Living room", power_state="unknown")
    client, _ = _client()
    runtime.device_state = _state(vizio_tvs=[tv])
    runtime.discovery_error = None

    response = client.post("/v1/ui/devices/vizio/192.168.1.10/toggle")

    assert response.status_code == HTTPStatus.OK
    assert tv.calls == ["off"]
    assert response.json()["device"]["state"] == "off"


def test_ui_device_toggle_endpoint_appears_in_openapi_schema() -> None:
    client, app = _client()
    paths = app.openapi()["paths"]
    assert "/v1/ui/devices/{family_id}/{device_id}/toggle" in paths
