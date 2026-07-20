"""Tests for Vizio UI action endpoints and helpers."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.api.ui_state import (
    build_ui_state,
    build_vizio_device_view,
    bulk_off_global_apply,
    bulk_off_vizio_apply,
    find_vizio_by_id,
)
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.vizio_device_manager import VizioDeviceManager


class _FakeVizioTv:
    def __init__(self, device_id: str, label: str, *, is_on: bool) -> None:
        self.identifier = device_id
        self.mac_address = device_id if ":" in device_id else "aa:bb:cc:dd:ee:ff"
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    def ui_power_state(self) -> str:
        return "on" if self.is_on else "off"

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def _kasa_mgr_empty() -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = ()
    mgr.get_device_by_alias.return_value = None
    return cast(KasaDeviceManager, mgr)


def _vizio_mgr(tvs: list[_FakeVizioTv]) -> VizioDeviceManager:
    mgr = MagicMock(spec=VizioDeviceManager)
    mgr.tvs = tuple(tvs)
    return cast(VizioDeviceManager, mgr)


def _state(
    *,
    vizio_tvs: list[_FakeVizioTv],
    cache_path: Path | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr_empty(),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=_vizio_mgr(vizio_tvs),
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def test_build_ui_state_includes_vizio_family() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    payload = build_ui_state(_state(vizio_tvs=[tv]), cache_path=None)
    assert len(payload.families) == 1
    assert payload.families[0].id == "vizio"
    assert payload.families[0].devices[0].compact_icon == "tv"
    assert payload.families[0].devices[0].state == "on"


def test_find_vizio_by_id_returns_none_for_blank_id() -> None:
    mgr = _vizio_mgr([_FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)])
    assert find_vizio_by_id(mgr, "   ") is None


@pytest.mark.asyncio
async def test_bulk_off_vizio_apply_turns_off_only_on_tvs() -> None:
    on_tv = _FakeVizioTv("192.168.1.10", "On TV", is_on=True)
    off_tv = _FakeVizioTv("192.168.1.11", "Off TV", is_on=False)
    state = _state(vizio_tvs=[on_tv, off_tv])
    affected, skipped = await bulk_off_vizio_apply(state)
    assert affected == ["192.168.1.10"]
    assert skipped == []
    assert on_tv.calls == ["off"]
    assert off_tv.calls == []


@pytest.mark.asyncio
async def test_bulk_off_global_apply_includes_vizio() -> None:
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    state = _state(vizio_tvs=[tv])
    affected, skipped = await bulk_off_global_apply(state, cache_path=None)
    assert ("vizio", "192.168.1.10") in affected
    assert skipped == []
    assert tv.calls == ["off"]


def test_vizio_toggle_endpoint_turns_on(tmp_path: Path) -> None:
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=False)
    state = _state(vizio_tvs=[tv], cache_path=tmp_path / "cache.sqlite")
    client, app = _client()
    runtime.device_state = state
    response = client.post(
        "/v1/ui/vizio/tvs/192.168.1.10/toggle",
        json={"on": True},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["device"]["state"] == "on"
    assert tv.calls == ["on"]


def test_vizio_preference_route_accepts_vizio_family(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    state = _state(vizio_tvs=[tv], cache_path=db)
    client, _app = _client()
    runtime.device_state = state
    response = client.put(
        "/v1/ui/preferences/vizio/192.168.1.10",
        json={"exclude_from_global": True, "hide_on_mobile": False},
    )
    assert response.status_code == HTTPStatus.OK
    rows = device_discovery_store.load_ui_preferences(db)
    assert ("vizio", "192.168.1.10", True, False) in rows


def test_build_vizio_device_view_reflects_exclusion(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_ui_preference(
        db,
        backend="vizio",
        canonical_key="192.168.1.10",
        exclude_from_global=True,
        hide_on_mobile=True,
    )
    tv = _FakeVizioTv("192.168.1.10", "Kitchen TV", is_on=True)
    state = _state(vizio_tvs=[tv], cache_path=db)
    assert state.vizio_mgr is not None
    view = build_vizio_device_view(
        state.vizio_mgr,
        device_id="192.168.1.10",
        cache_path=db,
    )
    assert view.exclude_from_global is True
    assert view.hide_on_mobile is True
    assert view.compact_icon == "tv"
