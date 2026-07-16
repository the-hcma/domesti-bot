"""Tests for :mod:`app.discovery_cache_sync` and UI state wiring."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import pytest
from fastapi.testclient import TestClient
from kasa.deviceconfig import DeviceConfig

from app import device_discovery_store
from app.api.app import create_app
from app.device_enums import DeviceFamilyId
from app.discovery_cache_sync import maybe_sync_discovery_cache
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.sonos_device_manager import SonosDeviceManager


def _xor_cfg(host: str) -> dict:
    return {
        "host": host,
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }


def _mock_device(host: str, alias: str, cfg: dict) -> MagicMock:
    mock_dev = MagicMock()
    mock_dev.host = host
    mock_dev.alias = alias
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock()
    mock_dev.disconnect = AsyncMock()
    return mock_dev


def _state(
    mgr: KasaDeviceManager,
    *,
    cache_path: Path | None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


@pytest.mark.asyncio
async def test_maybe_sync_noop_when_host_sets_match(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg, False)],
    )
    mock_dev = _mock_device("192.168.1.10", "Desk", cfg)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock()),
    ):
        await mgr.fetch()

    runtime.reset()
    with patch.object(mgr, "reload_from_cache", AsyncMock()) as reload:
        changed = await maybe_sync_discovery_cache(
            _state(mgr, cache_path=db)
        )
    assert changed is False
    reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_reloads_and_restarts_watchers_on_drift(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False)],
    )

    async def _connect(cfg: Any, *, credentials: Any, timeout: Any) -> MagicMock:
        del credentials, timeout
        return _mock_device(cfg.host, cfg.host, _xor_cfg(cfg.host))

    mgr = KasaDeviceManager(discovery_cache_path=db)
    mock_discover = AsyncMock(return_value={})
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=_connect),
        ),
        patch("app.kasa_device_manager.Discover.discover", mock_discover),
    ):
        await mgr.fetch()
        device_discovery_store.save_configs(
            db,
            [
                ("192.168.1.10", "Desk", cfg_a, False),
                ("192.168.1.20", "Lamp", cfg_b, False),
            ],
        )
        runtime.reset()
        with patch.object(
            runtime,
            "restart_device_state_watchers",
            AsyncMock(),
        ) as restart:
            changed = await maybe_sync_discovery_cache(
                _state(mgr, cache_path=db)
            )

    assert changed is True
    assert {kd._kDevice.host for kd in mgr.switches} == {
        "192.168.1.10",
        "192.168.1.20",
    }
    restart.assert_awaited_once()
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_skips_retry_after_failed_fingerprint(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False)],
    )
    mock_a = _mock_device("192.168.1.10", "Desk", cfg_a)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_a),
        ),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock()),
    ):
        await mgr.fetch()

    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Desk", cfg_a, False),
            ("192.168.1.20", "Broken", cfg_b, False),
        ],
    )

    async def _connect_fail(cfg: Any, *, credentials: Any, timeout: Any) -> MagicMock | None:
        del credentials, timeout
        if cfg.host == "192.168.1.10":
            return _mock_device("192.168.1.10", "Desk", cfg_a)
        return None

    runtime.reset()
    with patch(
        "app.kasa_device_manager._connect_from_saved_config",
        AsyncMock(side_effect=_connect_fail),
    ):
        first = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))
        with patch.object(mgr, "reload_from_cache", AsyncMock()) as reload:
            second = await maybe_sync_discovery_cache(
                _state(mgr, cache_path=db)
            )

    assert first is False
    assert second is False
    reload.assert_not_awaited()
    assert runtime.discovery_cache_sync_failed[DeviceFamilyId.KASA.value] == frozenset(
        {"192.168.1.10", "192.168.1.20"}
    )


@pytest.mark.asyncio
async def test_maybe_sync_sonos_reloads_without_udp(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "192.168.1.30", "Kitchen"),
            ("RINCON_B", "192.168.1.31", "Patio"),
        ],
    )
    mgr = MagicMock(spec=SonosDeviceManager)
    live = MagicMock()
    live.identifier = "RINCON_A"
    live._soco.ip_address = "192.168.1.30"
    mgr.players = (live,)
    mgr.reload_from_cache = AsyncMock(return_value=True)

    runtime.reset()
    with (
        patch.object(runtime, "restart_device_state_watchers", AsyncMock()) as restart,
        patch("app.sonos_device_manager.soco_discover") as discover,
    ):
        changed = await maybe_sync_discovery_cache(
            DeviceManagersState(
                kasa_mgr=MagicMock(spec=KasaDeviceManager),
                sonos_mgr=mgr,
                tailwind_mgr=None,
                androidtv_mgr=None,
                vizio_mgr=None,
                cache_path=db,
                args=argparse.Namespace(),
            )
        )

    assert changed is True
    mgr.reload_from_cache.assert_awaited_once()
    restart.assert_awaited_once()
    discover.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_sync_restarts_watchers_once_for_multi_family_drift(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(db, [("192.168.1.10", "Desk", cfg, False)])
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen")],
    )

    kasa = MagicMock(spec=KasaDeviceManager)
    kasa.switches = ()
    kasa.skipped_auth_hosts = ()
    kasa.reload_from_cache = AsyncMock(return_value=True)

    sonos = MagicMock(spec=SonosDeviceManager)
    sonos.players = ()
    sonos.reload_from_cache = AsyncMock(return_value=True)

    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Desk", cfg, False),
            ("192.168.1.20", "Lamp", cfg, False),
        ],
    )
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "192.168.1.30", "Kitchen"),
            ("RINCON_B", "192.168.1.31", "Patio"),
        ],
    )

    runtime.reset()
    with patch.object(runtime, "restart_device_state_watchers", AsyncMock()) as restart:
        changed = await maybe_sync_discovery_cache(
            DeviceManagersState(
                kasa_mgr=kasa,
                sonos_mgr=sonos,
                tailwind_mgr=None,
                androidtv_mgr=None,
                vizio_mgr=None,
                cache_path=db,
                args=argparse.Namespace(),
            )
        )

    assert changed is True
    kasa.reload_from_cache.assert_awaited_once()
    sonos.reload_from_cache.assert_awaited_once()
    restart.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_sync_keeps_kasa_when_cache_emptied(tmp_path: Path) -> None:
    """Empty SQLite is a no-op — keep a healthy UI map on a transient empty read."""

    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg, False)],
    )
    mock_dev = _mock_device("192.168.1.10", "Desk", cfg)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock()),
    ):
        await mgr.fetch()

    device_discovery_store.save_configs(db, [])
    runtime.reset()
    with (
        patch.object(runtime, "restart_device_state_watchers", AsyncMock()) as restart,
        patch.object(mgr, "reload_from_cache", AsyncMock()) as reload,
    ):
        changed = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))

    assert changed is False
    assert {kd._kDevice.host for kd in mgr.switches} == {"192.168.1.10"}
    reload.assert_not_awaited()
    restart.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_continues_after_family_exception(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(db, [("192.168.1.10", "Desk", cfg, False)])
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen")],
    )

    kasa = MagicMock(spec=KasaDeviceManager)
    kasa.switches = ()
    kasa.skipped_auth_hosts = ()
    kasa.reload_from_cache = AsyncMock(side_effect=RuntimeError("kasa boom"))

    sonos = MagicMock(spec=SonosDeviceManager)
    sonos.players = ()
    sonos.reload_from_cache = AsyncMock(return_value=True)

    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Desk", cfg, False),
            ("192.168.1.20", "Lamp", cfg, False),
        ],
    )

    runtime.reset()
    with patch.object(runtime, "restart_device_state_watchers", AsyncMock()) as restart:
        changed = await maybe_sync_discovery_cache(
            DeviceManagersState(
                kasa_mgr=kasa,
                sonos_mgr=sonos,
                tailwind_mgr=None,
                androidtv_mgr=None,
                vizio_mgr=None,
                cache_path=db,
                args=argparse.Namespace(),
            )
        )

    assert changed is True
    kasa.reload_from_cache.assert_awaited_once()
    sonos.reload_from_cache.assert_awaited_once()
    restart.assert_awaited_once()


def test_get_v1_ui_state_syncs_kasa_roster_from_cache(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False)],
    )

    async def _connect(cfg: Any, *, credentials: Any, timeout: Any) -> MagicMock:
        del credentials, timeout
        alias = "Desk" if cfg.host == "192.168.1.10" else "Lamp"
        return _mock_device(cfg.host, alias, _xor_cfg(cfg.host))

    mgr = KasaDeviceManager(discovery_cache_path=db)
    mock_discover = AsyncMock(return_value={})

    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=_connect),
        ),
        patch("app.kasa_device_manager.Discover.discover", mock_discover),
    ):
        anyio.run(mgr.fetch)
        device_discovery_store.save_configs(
            db,
            [
                ("192.168.1.10", "Desk", cfg_a, False),
                ("192.168.1.20", "Lamp", cfg_b, False),
            ],
        )

        runtime.reset()
        client = TestClient(create_app(argparse.Namespace()))
        runtime.device_state = _state(mgr, cache_path=db)
        runtime.discovery_error = None
        with patch.object(runtime, "restart_device_state_watchers", AsyncMock()):
            response = client.get("/v1/ui/state")

    assert response.status_code == HTTPStatus.OK
    payload = response.json()
    kasa = next(f for f in payload["families"] if f["id"] == "kasa")
    assert {d["id"] for d in kasa["devices"]} == {
        "192.168.1.10",
        "192.168.1.20",
    }
    mock_discover.assert_not_awaited()
