"""Tests for :meth:`KasaDeviceManager.rediscover` (no hardware)."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import kasa_discovery_store
from kasa_device_manager import KasaDeviceManager


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
    kasa_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Desk lamp", cfg)],
    )

    mgr = KasaDeviceManager(discovery_cache_path=db, force_discovery=False)

    mock_discover = AsyncMock(return_value={})
    with patch("kasa_device_manager.Discover.discover", mock_discover):
        await mgr.rediscover()

    mock_discover.assert_awaited()
    assert mgr.switches == ()
