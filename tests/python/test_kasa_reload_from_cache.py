"""Tests for :meth:`KasaDeviceManager.reload_from_cache` (no UDP)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kasa.credentials import Credentials
from kasa.deviceconfig import DeviceConfig

from app import device_discovery_store
from app.kasa_device_manager import KasaDeviceManager


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
    mock_dev.mac = None
    mock_dev.sys_info = {}
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock()
    mock_dev.disconnect = AsyncMock()
    return mock_dev


@pytest.mark.asyncio
async def test_reload_from_cache_never_calls_discover(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [("192.168.1.10", "Desk", cfg_a, False)],
    )

    mock_a = _mock_device("192.168.1.10", "Desk", cfg_a)
    mgr = KasaDeviceManager(discovery_cache_path=db)
    mock_discover = AsyncMock(return_value={})
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_a),
        ),
        patch("app.kasa_device_manager.Discover.discover", mock_discover),
    ):
        await mgr.fetch()
        assert len(mgr.switches) == 1

        device_discovery_store.save_configs(
            db,
            [
                ("192.168.1.10", "Desk", cfg_a, False),
                ("192.168.1.20", "Lamp", cfg_b, False),
            ],
        )
        mock_b = _mock_device("192.168.1.20", "Lamp", cfg_b)

        async def _connect(cfg, *, credentials, timeout):
            del credentials, timeout
            if cfg.host == "192.168.1.10":
                return _mock_device("192.168.1.10", "Desk", cfg_a)
            if cfg.host == "192.168.1.20":
                return mock_b
            return None

        with patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=_connect),
        ):
            ok = await mgr.reload_from_cache()

    assert ok is True
    assert {kd._kDevice.host for kd in mgr.switches} == {
        "192.168.1.10",
        "192.168.1.20",
    }
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_from_cache_removes_host_dropped_from_sqlite(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_b = _xor_cfg("192.168.1.20")
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Desk", cfg_a, False),
            ("192.168.1.20", "Lamp", cfg_b, False),
        ],
    )

    async def _connect(cfg, *, credentials, timeout):
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
        assert len(mgr.switches) == 2

        device_discovery_store.save_configs(
            db,
            [("192.168.1.10", "Desk", cfg_a, False)],
        )
        ok = await mgr.reload_from_cache()

    assert ok is True
    assert [kd._kDevice.host for kd in mgr.switches] == ["192.168.1.10"]
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_from_cache_clears_klap_auth_for_dropped_host(tmp_path) -> None:
    """Hosts removed from SQLite must not keep a stale KLAP-auth marker."""

    db = tmp_path / "cached.sqlite"
    cfg_a = _xor_cfg("192.168.1.10")
    cfg_klap = _xor_cfg("192.168.1.99")
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Desk", cfg_a, False),
            ("192.168.1.99", "KlapPlug", cfg_klap, True),
        ],
    )

    async def _connect(cfg, *, credentials, timeout):
        del credentials, timeout
        return _mock_device(cfg.host, cfg.host, _xor_cfg(cfg.host))

    mgr = KasaDeviceManager(
        discovery_cache_path=db,
        credentials=Credentials(username="a@example.com", password="x"),
    )
    with patch(
        "app.kasa_device_manager._connect_from_saved_config",
        AsyncMock(side_effect=_connect),
    ):
        await mgr.fetch()
        assert "192.168.1.99" in mgr.hosts_requiring_klap_auth

        device_discovery_store.save_configs(
            db,
            [("192.168.1.10", "Desk", cfg_a, False)],
        )
        ok = await mgr.reload_from_cache()

    assert ok is True
    assert "192.168.1.99" not in mgr.hosts_requiring_klap_auth
    assert [kd._kDevice.host for kd in mgr.switches] == ["192.168.1.10"]


@pytest.mark.asyncio
async def test_reload_from_cache_keeps_prior_map_when_reconnect_fails(
    tmp_path,
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
    mock_discover = AsyncMock(return_value={})
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_a),
        ),
        patch("app.kasa_device_manager.Discover.discover", mock_discover),
    ):
        await mgr.fetch()
        prior_label = mgr.switches[0].preferred_label

        device_discovery_store.save_configs(
            db,
            [
                ("192.168.1.10", "Desk", cfg_a, False),
                ("192.168.1.20", "Broken", cfg_b, False),
            ],
        )

        async def _connect(cfg, *, credentials, timeout):
            del credentials, timeout
            if cfg.host == "192.168.1.10":
                return _mock_device("192.168.1.10", "Desk", cfg_a)
            return None

        with patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=_connect),
        ):
            ok = await mgr.reload_from_cache()

    assert ok is False
    assert len(mgr.switches) == 1
    assert mgr.switches[0].preferred_label == prior_label
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_reload_from_cache_does_not_persist_discovery_table(tmp_path) -> None:
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
        patch.object(
            mgr,
            "_persist_discovery_cache",
            MagicMock(),
        ) as persist,
    ):
        await mgr.fetch()
        persist.reset_mock()
        ok = await mgr.reload_from_cache()

    assert ok is True
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_reload_from_cache_keeps_prior_map_when_sqlite_empty(tmp_path) -> None:
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
        assert len(mgr.switches) == 1
        device_discovery_store.save_configs(db, [])
        ok = await mgr.reload_from_cache()

    assert ok is False
    assert {kd._kDevice.host for kd in mgr.switches} == {"192.168.1.10"}
