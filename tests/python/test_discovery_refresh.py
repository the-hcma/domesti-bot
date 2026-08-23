"""Hermetic tests for multi-family discovery refresh + new-device reporting."""

from __future__ import annotations

import argparse
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest

from app.device_manager import NotInitializedError
from app.discovery_refresh import (
    NEW_DEVICE_FOUND_PREFIX,
    discovery_settings_status,
    refresh_all_device_discovery,
    snapshot_family_devices,
)
from app.domesti_bot_cli import DeviceManagersState


def _device(device_id: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(identifier=device_id, preferred_label=label)


def _kasa_mgr(*devices: SimpleNamespace) -> MagicMock:
    mgr = MagicMock()
    mgr.switches = list(devices)
    mgr.last_discovery_source = "discovery"
    mgr.rediscover = AsyncMock()
    return mgr


def _state(*, kasa_mgr: MagicMock | None = None) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=kasa_mgr or _kasa_mgr(),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )


def test_snapshot_family_devices_formats_display() -> None:
    mgr = _kasa_mgr(_device("aa:bb:cc:dd:ee:01", "Kitchen Plug"))
    state = _state(kasa_mgr=mgr)
    snap = snapshot_family_devices(state, "kasa")
    assert "aa:bb:cc:dd:ee:01" in snap
    assert snap["aa:bb:cc:dd:ee:01"].display == "Kitchen Plug (aa:bb:cc:dd:ee:01)"
    assert snap["aa:bb:cc:dd:ee:01"].preferred_label == "Kitchen Plug"


def test_discovery_settings_status_marks_unloaded_families() -> None:
    state = _state(kasa_mgr=_kasa_mgr(_device("aa:bb:cc:dd:ee:01", "Kitchen Plug")))
    status = discovery_settings_status(state)
    by_id = {family.family_id: family for family in status.families}
    assert by_id["kasa"].available is True
    assert by_id["kasa"].device_count == 1
    assert by_id["kasa"].last_discovery_source == "discovery"
    assert by_id["sonos"].available is False
    assert by_id["sonos"].device_count == 0


@pytest.mark.asyncio
async def test_refresh_reports_new_kasa_device(monkeypatch: pytest.MonkeyPatch) -> None:
    existing = _device("aa:bb:cc:dd:ee:01", "Kitchen Plug")
    new = _device("aa:bb:cc:dd:ee:02", "Porch Plug")
    mgr = _kasa_mgr(existing)

    async def _rediscover() -> None:
        mgr.switches = [existing, new]

    mgr.rediscover = AsyncMock(side_effect=_rediscover)
    state = _state(kasa_mgr=mgr)

    restart = AsyncMock()
    monkeypatch.setattr("app.server_runtime.runtime.device_state", state)
    monkeypatch.setattr(
        "app.server_runtime.runtime.restart_device_state_watchers",
        restart,
    )

    result = await refresh_all_device_discovery(state, restart_watchers=True)
    assert len(result.new_devices) == 1
    assert result.new_devices[0].device_id == "aa:bb:cc:dd:ee:02"
    assert result.new_devices[0].display == "Porch Plug (aa:bb:cc:dd:ee:02)"
    assert NEW_DEVICE_FOUND_PREFIX.startswith("New device found")
    kasa = next(family for family in result.families if family.family_id == "kasa")
    assert kasa.ok is True
    assert kasa.device_count == 2
    assert len(kasa.new_devices) == 1
    sonos = next(family for family in result.families if family.family_id == "sonos")
    assert sonos.skipped is True
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_skips_watcher_restart_when_no_live_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = _kasa_mgr(_device("aa:bb:cc:dd:ee:01", "Kitchen Plug"))
    mgr.rediscover = AsyncMock()
    state = _state(kasa_mgr=mgr)
    restart = AsyncMock()
    monkeypatch.setattr("app.server_runtime.runtime.device_state", None)
    monkeypatch.setattr(
        "app.server_runtime.runtime.restart_device_state_watchers",
        restart,
    )
    await refresh_all_device_discovery(state, restart_watchers=True)
    restart.assert_not_awaited()


def test_discovery_settings_status_marks_uninitialized_unavailable() -> None:
    mgr = MagicMock()
    mgr.last_discovery_source = None
    type(mgr).switches = PropertyMock(side_effect=NotInitializedError)
    status = discovery_settings_status(_state(kasa_mgr=mgr))
    by_id = {family.family_id: family for family in status.families}
    assert by_id["kasa"].available is False
    assert by_id["kasa"].device_count == 0


@pytest.mark.asyncio
async def test_refresh_skips_new_device_diff_when_before_uninitialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uninitialized before-snapshot must not announce the whole roster as new."""
    existing = _device("aa:bb:cc:dd:ee:01", "Kitchen Plug")
    new = _device("aa:bb:cc:dd:ee:02", "Porch Plug")
    mgr = MagicMock()
    mgr.last_discovery_source = "discovery"
    type(mgr).switches = PropertyMock(side_effect=NotInitializedError)

    async def _rediscover() -> None:
        type(mgr).switches = PropertyMock(return_value=[existing, new])

    mgr.rediscover = AsyncMock(side_effect=_rediscover)
    state = _state(kasa_mgr=mgr)
    monkeypatch.setattr("app.server_runtime.runtime.device_state", None)

    result = await refresh_all_device_discovery(state, restart_watchers=False)
    assert result.new_devices == ()
    kasa = next(family for family in result.families if family.family_id == "kasa")
    assert kasa.ok is True
    assert kasa.device_count == 2
    assert kasa.new_devices == ()
