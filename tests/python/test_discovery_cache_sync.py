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
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime
from app.sonos_device_manager import SonosDeviceManager

# Hermetic ARP stub maps these hosts to stable MACs (see tests/python/conftest.py).
_MAC_10 = "aa:bb:c0:a8:01:0a"  # 192.168.1.10
_MAC_14 = "aa:bb:c0:a8:01:14"  # 192.168.1.20
_MAC_1E = "aa:bb:c0:a8:01:1e"  # 192.168.1.30
_MAC_1F = "aa:bb:c0:a8:01:1f"  # 192.168.1.31


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


def _sonos_player(*, rincon_uid: str, host: str, mac: str = "aa:bb:cc:dd:ee:ff") -> MagicMock:
    """Live Sonos zone shaped like post-MAC-primary devices."""

    live = MagicMock()
    live.identifier = mac
    live.rincon_uid = rincon_uid
    live.host = host
    return live


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
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


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


@pytest.mark.asyncio
async def test_maybe_sync_continues_after_family_exception(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(db, [("192.168.1.10", "Desk", cfg, False, _MAC_10)])
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen", _MAC_1E)],
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
            ("192.168.1.10", "Desk", cfg, False, _MAC_10),
            ("192.168.1.20", "Lamp", cfg, False, _MAC_14),
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
                ep1_mgr=None,
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
        [("192.168.1.10", "Desk", cfg, False, _MAC_10)],
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
async def test_maybe_sync_kasa_noop_when_same_mac_moves_ip(tmp_path: Path) -> None:
    """DHCP host change alone must not look like roster drift."""

    db = tmp_path / "cached.sqlite"
    cfg_old = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_old, False, _MAC_10)],
    )
    mock_dev = _mock_device("192.168.1.10", "Desk", cfg_old)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock()),
    ):
        await mgr.fetch()

    cfg_new = _xor_cfg("192.168.1.99")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.99", "Desk", cfg_new, False, _MAC_10)],
    )
    runtime.reset()
    with patch.object(mgr, "reload_from_cache", AsyncMock()) as reload:
        changed = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))
    assert changed is False
    reload.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_noop_when_mac_sets_match(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg, False, _MAC_10)],
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
        changed = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))
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
        [("192.168.1.10", "Desk", cfg_a, False, _MAC_10)],
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
                ("192.168.1.10", "Desk", cfg_a, False, _MAC_10),
                ("192.168.1.20", "Lamp", cfg_b, False, _MAC_14),
            ],
        )
        runtime.reset()
        with patch.object(
            runtime,
            "restart_device_state_watchers",
            AsyncMock(),
        ) as restart:
            changed = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))

    assert changed is True
    assert {kd._kDevice.host for kd in mgr.switches} == {
        "192.168.1.10",
        "192.168.1.20",
    }
    restart.assert_awaited_once()
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_restarts_watchers_once_for_multi_family_drift(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = _xor_cfg("192.168.1.10")
    device_discovery_store.save_configs(db, [("192.168.1.10", "Desk", cfg, False, _MAC_10)])
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen", _MAC_1E)],
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
            ("192.168.1.10", "Desk", cfg, False, _MAC_10),
            ("192.168.1.20", "Lamp", cfg, False, _MAC_14),
        ],
    )
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "192.168.1.30", "Kitchen", _MAC_1E),
            ("RINCON_B", "192.168.1.31", "Patio", _MAC_1F),
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
                ep1_mgr=None,
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
async def test_maybe_sync_skips_retry_after_failed_fingerprint(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False, _MAC_10)],
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
            ("192.168.1.10", "Desk", cfg_a, False, _MAC_10),
            ("192.168.1.20", "Broken", cfg_b, False, _MAC_14),
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
            second = await maybe_sync_discovery_cache(_state(mgr, cache_path=db))

    assert first is False
    assert second is False
    reload.assert_not_awaited()
    assert runtime.discovery_cache_sync_failed[DeviceFamilyId.KASA.value] == frozenset({_MAC_10, _MAC_14})


@pytest.mark.asyncio
async def test_maybe_sync_sonos_noop_when_rincon_case_differs(tmp_path: Path) -> None:
    """Upper/lower RINCON spelling must not look like roster drift."""

    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("rincon_a", "192.168.1.30", "Kitchen", _MAC_1E)],
    )
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = (_sonos_player(rincon_uid="RINCON_A", host="192.168.1.30", mac=_MAC_1E),)
    mgr.reload_from_cache = AsyncMock()

    runtime.reset()
    changed = await maybe_sync_discovery_cache(
        DeviceManagersState(
            kasa_mgr=MagicMock(spec=KasaDeviceManager),
            sonos_mgr=mgr,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        )
    )

    assert changed is False
    mgr.reload_from_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_sonos_noop_when_rincon_sets_match(tmp_path: Path) -> None:
    """Same RINCON roster is a no-op even when live host IPs differ from cache."""

    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_A", "192.168.1.30", "Kitchen", _MAC_1E)],
    )
    mgr = MagicMock(spec=SonosDeviceManager)
    # Live host deliberately differs from the cached IP.
    mgr.players = (_sonos_player(rincon_uid="RINCON_A", host="192.168.1.99", mac=_MAC_1E),)
    mgr.reload_from_cache = AsyncMock()

    runtime.reset()
    changed = await maybe_sync_discovery_cache(
        DeviceManagersState(
            kasa_mgr=MagicMock(spec=KasaDeviceManager),
            sonos_mgr=mgr,
            tailwind_mgr=None,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        )
    )

    assert changed is False
    mgr.reload_from_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_sync_sonos_reloads_without_udp(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_A", "192.168.1.30", "Kitchen", _MAC_1E),
            ("RINCON_B", "192.168.1.31", "Patio", _MAC_1F),
        ],
    )
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = (_sonos_player(rincon_uid="RINCON_A", host="192.168.1.30", mac=_MAC_1E),)
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
                ep1_mgr=None,
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
async def test_maybe_sync_tailwind_noop_when_same_hub_mac_moves_ip(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    device_discovery_store.save_tailwind_host(db, "192.168.1.50", mac=_MAC_10)
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.hub_mac = _MAC_10
    mgr.host = "192.168.1.99"
    mgr.reload_from_cache = AsyncMock()

    runtime.reset()
    changed = await maybe_sync_discovery_cache(
        DeviceManagersState(
            kasa_mgr=MagicMock(spec=KasaDeviceManager),
            sonos_mgr=None,
            tailwind_mgr=mgr,
            androidtv_mgr=None,
            ep1_mgr=None,
            vizio_mgr=None,
            cache_path=db,
            args=argparse.Namespace(),
        )
    )

    assert changed is False
    mgr.reload_from_cache.assert_not_awaited()


def test_get_v1_ui_state_syncs_kasa_roster_from_cache(tmp_path: Path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False, _MAC_10)],
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
                ("192.168.1.10", "Desk", cfg_a, False, _MAC_10),
                ("192.168.1.20", "Lamp", cfg_b, False, _MAC_14),
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
        _MAC_10,
        _MAC_14,
    }
    mock_discover.assert_not_awaited()
