"""Tests for :meth:`KasaDeviceManager.rediscover` (no hardware)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import device_discovery_store
from app.kasa_device_manager import KasaDeviceManager


@pytest.mark.asyncio
async def test_rediscover_uses_udp_not_sqlite_cache(tmp_path) -> None:
    db = tmp_path / "cached.sqlite"
    cfg = {
        "host": "192.168.1.50",
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }
    device_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Desk lamp", cfg)],
    )

    mgr = KasaDeviceManager(discovery_cache_path=db, force_discovery=False)

    mock_discover = AsyncMock(return_value={})
    with patch("app.kasa_device_manager.Discover.discover", mock_discover):
        await mgr.rediscover()

    mock_discover.assert_awaited()
    assert mgr.switches == ()


@pytest.mark.asyncio
async def test_fetch_cache_hit_refreshes_alias_from_device_update(tmp_path) -> None:
    """Cache reconnect must call ``update()`` so renamed Kasa aliases reach the UI."""

    db = tmp_path / "cached.sqlite"
    cfg = {
        "host": "192.168.1.50",
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }
    device_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Old plug name", cfg)],
    )

    mock_dev = MagicMock()
    mock_dev.host = "192.168.1.50"
    mock_dev.alias = "Old plug name"
    mock_dev.is_on = False
    mock_dev.config.to_dict_control_credentials.return_value = cfg

    async def _update() -> None:
        mock_dev.alias = "New plug name"

    mock_dev.update = AsyncMock(side_effect=_update)

    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch("app.kasa_device_manager.Discover.discover", AsyncMock(return_value={})),
    ):
        await mgr.fetch()

    assert mgr.last_discovery_source == "cache"
    assert len(mgr.switches) == 1
    assert mgr.switches[0].preferred_label == "New plug name"
