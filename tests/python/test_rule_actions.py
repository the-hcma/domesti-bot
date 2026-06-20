"""Hermetic tests for automation rule device action dispatch."""

from __future__ import annotations

import argparse
from typing import cast
from unittest.mock import MagicMock

import pytest

from app.api.schemas import RuleDeviceActionOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.rule_actions import (
    RuleActionDispatchError,
    dispatch_rule_device_actions,
    resolve_kasa_host_by_label,
)
from app.sonos_device_manager import SonosDeviceManager
from app.vizio_device_manager import VizioDeviceManager


class _FakeKasa:
    def __init__(self, host: str, label: str) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.identifier = host
        self.preferred_label = label
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")

    async def turn_off(self) -> None:
        self.calls.append("off")


class _FakeVizioTv:
    def __init__(self, device_id: str, label: str, *, is_on: bool) -> None:
        self.identifier = device_id
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True

    def ui_power_state(self) -> str:
        return "on" if self.is_on else "off"


def _device_state(
    kasa_mgr: KasaDeviceManager | None = None,
    *,
    vizio_mgr: VizioDeviceManager | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=kasa_mgr or _kasa_mgr([]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=vizio_mgr,
        cache_path=None,
        args=argparse.Namespace(),
    )


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    return cast(KasaDeviceManager, mgr)


def _vizio_mgr(tvs: list[_FakeVizioTv]) -> VizioDeviceManager:
    mgr = MagicMock(spec=VizioDeviceManager)
    mgr.tvs = tuple(tvs)
    return cast(VizioDeviceManager, mgr)


def test_resolve_kasa_host_by_label_matches_display_name() -> None:
    mgr = _kasa_mgr([_FakeKasa("192.168.1.10", "Garage")])
    assert resolve_kasa_host_by_label(mgr, "Garage") == "192.168.1.10"


def test_resolve_kasa_host_by_label_matches_host() -> None:
    mgr = _kasa_mgr([_FakeKasa("192.168.1.10", "Garage")])
    assert resolve_kasa_host_by_label(mgr, "192.168.1.10") == "192.168.1.10"


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_turns_off_vizio_by_label() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    state = _device_state(vizio_mgr=_vizio_mgr([tv]))
    errors = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.VIZIO,
                device_id="Kitchen TV",
                action=RuleDeviceActionType.TURN_OFF,
            ),
        ],
    )
    assert errors == []
    assert tv.calls == ["off"]


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_turns_on_by_label() -> None:
    device = _FakeKasa("192.168.1.20", "Front door lights")
    state = _device_state(_kasa_mgr([device]))
    errors = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Front door lights",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
    )
    assert errors == []
    assert device.calls == ["on"]


@pytest.mark.asyncio
async def test_dispatch_rule_device_actions_collects_unknown_device_error() -> None:
    state = _device_state(_kasa_mgr([]))
    errors = await dispatch_rule_device_actions(
        state,
        [
            RuleDeviceActionOut(
                family_id=DeviceFamilyId.KASA,
                device_id="Missing plug",
                action=RuleDeviceActionType.TURN_ON,
            ),
        ],
    )
    assert len(errors) == 1
    assert "Unknown Kasa device" in errors[0]


def test_resolve_kasa_host_by_label_raises_on_ambiguous_label() -> None:
    mgr = _kasa_mgr(
        [
            _FakeKasa("192.168.1.10", "Garage"),
            _FakeKasa("192.168.1.11", "Garage"),
        ]
    )
    with pytest.raises(RuleActionDispatchError, match="Ambiguous Kasa device"):
        resolve_kasa_host_by_label(mgr, "Garage")
