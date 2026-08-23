"""Hermetic tests for multi-family discovery refresh + new-device reporting."""

from __future__ import annotations

import argparse
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.device_manager import NotInitializedError
from app.discovery_refresh import (
    NEW_DEVICE_FOUND_PREFIX,
    discovery_settings_status,
    refresh_all_device_discovery,
    snapshot_family_devices,
)
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import SonosDeviceManager


class _FakeKasaMgr:
    """Per-instance Kasa stand-in (avoids MagicMock class-attribute leakage)."""

    def __init__(self, *devices: SimpleNamespace) -> None:
        self._switches: list[SimpleNamespace] | None = list(devices)
        self.last_discovery_source: str | None = "discovery"
        self.rediscover = AsyncMock()

    @property
    def switches(self) -> list[SimpleNamespace]:
        if self._switches is None:
            raise NotInitializedError
        return self._switches

    @switches.setter
    def switches(self, value: list[SimpleNamespace] | None) -> None:
        self._switches = value


class _FakeSonosMgr:
    """Per-instance Sonos stand-in."""

    def __init__(self, *devices: SimpleNamespace) -> None:
        self.players: list[SimpleNamespace] = list(devices)
        self.last_discovery_source: str | None = "discovery"
        self.rediscover = AsyncMock()


def _device(device_id: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(identifier=device_id, preferred_label=label)


def _kasa_mgr(*devices: SimpleNamespace) -> _FakeKasaMgr:
    return _FakeKasaMgr(*devices)


def _sonos_mgr(*devices: SimpleNamespace) -> _FakeSonosMgr:
    return _FakeSonosMgr(*devices)


def _state(
    *,
    cache_path: Path | None = None,
    kasa_mgr: Any | None = None,
    sonos_mgr: Any | None = None,
    tailwind_mgr: Any | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=cast(KasaDeviceManager, kasa_mgr or _kasa_mgr()),
        sonos_mgr=cast(SonosDeviceManager | None, sonos_mgr),
        tailwind_mgr=tailwind_mgr,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=cache_path,
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
    assert len(by_id["kasa"].devices) == 1
    assert by_id["kasa"].devices[0].display == "Kitchen Plug (aa:bb:cc:dd:ee:01)"
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
    mgr = _kasa_mgr()
    mgr.switches = None
    mgr.last_discovery_source = None
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
    mgr = _kasa_mgr()
    mgr.switches = None

    async def _rediscover() -> None:
        mgr.switches = [existing, new]

    mgr.rediscover = AsyncMock(side_effect=_rediscover)
    state = _state(kasa_mgr=mgr)
    monkeypatch.setattr("app.server_runtime.runtime.device_state", None)

    result = await refresh_all_device_discovery(state, restart_watchers=False)
    assert result.new_devices == ()
    kasa = next(family for family in result.families if family.family_id == "kasa")
    assert kasa.ok is True
    assert kasa.device_count == 2
    assert kasa.new_devices == ()


@pytest.mark.asyncio
async def test_refresh_failure_reports_post_failure_count_and_restarts_when_other_ok(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = _device("aa:bb:cc:dd:ee:01", "Kitchen Plug")
    kasa = _kasa_mgr(existing)
    sonos = _sonos_mgr(_device("aa:bb:cc:dd:ee:10", "Living Room"))

    async def _kasa_fail() -> None:
        kasa.switches = None
        raise RuntimeError("udp down")

    kasa.rediscover = AsyncMock(side_effect=_kasa_fail)
    state = _state(kasa_mgr=kasa, sonos_mgr=sonos)
    restart = AsyncMock()
    monkeypatch.setattr("app.server_runtime.runtime.device_state", state)
    monkeypatch.setattr(
        "app.server_runtime.runtime.restart_device_state_watchers",
        restart,
    )

    result = await refresh_all_device_discovery(state, restart_watchers=True)
    assert result.new_devices == ()
    kasa_result = next(family for family in result.families if family.family_id == "kasa")
    assert kasa_result.ok is False
    assert kasa_result.device_count == 0
    assert "udp down" in (kasa_result.error or "")
    sonos_result = next(family for family in result.families if family.family_id == "sonos")
    assert sonos_result.ok is True
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_failure_keeps_readable_roster_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When rediscover raises but the roster stays readable (cache fallback / EP1 restore)."""
    existing = _device("aa:bb:cc:dd:ee:01", "Kitchen Plug")
    kasa = _kasa_mgr(existing)
    sonos = _sonos_mgr(_device("aa:bb:cc:dd:ee:10", "Living Room"))
    kasa.rediscover = AsyncMock(side_effect=RuntimeError("udp down; cache reconnect ok"))
    state = _state(kasa_mgr=kasa, sonos_mgr=sonos)
    restart = AsyncMock()
    monkeypatch.setattr("app.server_runtime.runtime.device_state", state)
    monkeypatch.setattr(
        "app.server_runtime.runtime.restart_device_state_watchers",
        restart,
    )

    result = await refresh_all_device_discovery(state, restart_watchers=True)
    kasa_result = next(family for family in result.families if family.family_id == "kasa")
    assert kasa_result.ok is False
    assert kasa_result.device_count == 1
    assert kasa_result.new_devices == ()
    assert kasa_result.source is None
    assert "udp down" in (kasa_result.error or "")
    assert result.new_devices == ()
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_skips_watcher_restart_when_all_families_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kasa = _kasa_mgr(_device("aa:bb:cc:dd:ee:01", "Kitchen Plug"))
    sonos = _sonos_mgr(_device("aa:bb:cc:dd:ee:10", "Living Room"))
    kasa.rediscover = AsyncMock(side_effect=RuntimeError("kasa boom"))
    sonos.rediscover = AsyncMock(side_effect=RuntimeError("sonos boom"))
    state = _state(kasa_mgr=kasa, sonos_mgr=sonos)
    restart = AsyncMock()
    monkeypatch.setattr("app.server_runtime.runtime.device_state", state)
    monkeypatch.setattr(
        "app.server_runtime.runtime.restart_device_state_watchers",
        restart,
    )
    result = await refresh_all_device_discovery(state, restart_watchers=True)
    assert all(not family.ok or family.skipped for family in result.families)
    restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_persists_gotailwind_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache = tmp_path / "discovery.sqlite"
    tailwind = MagicMock()
    tailwind.doors = []
    tailwind.host = "192.168.1.40"
    tailwind.last_discovery_source = "discovery"
    tailwind.rediscover = AsyncMock()
    state = _state(cache_path=cache, kasa_mgr=_kasa_mgr(), tailwind_mgr=tailwind)
    save = MagicMock()
    monkeypatch.setattr(
        "app.discovery_refresh.device_discovery_store.save_tailwind_host",
        save,
    )
    monkeypatch.setattr("app.server_runtime.runtime.device_state", None)

    result = await refresh_all_device_discovery(state, restart_watchers=False)
    save.assert_called_once_with(cache, "192.168.1.40")
    gotailwind = next(family for family in result.families if family.family_id == "gotailwind")
    assert gotailwind.ok is True
