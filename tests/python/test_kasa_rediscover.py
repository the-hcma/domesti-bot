"""Tests for :meth:`KasaDeviceManager.rediscover` (no hardware)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from kasa.deviceconfig import DeviceConfig
from kasa.exceptions import _ConnectionError

from app import device_discovery_store
from app.device_manager import NotInitializedError
from app.kasa_device_manager import KasaDeviceManager


@pytest.mark.asyncio
async def test_rediscover_restores_from_cache_when_udp_raises(tmp_path) -> None:
    """A failed UDP sweep must not leave the manager empty when cache reconnect works."""

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
        [("192.168.1.50", "Desk lamp", cfg, False)],
    )

    mock_dev = MagicMock()
    mock_dev.host = "192.168.1.50"
    mock_dev.alias = "Desk lamp"
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock()
    mock_dev.disconnect = AsyncMock()

    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(side_effect=RuntimeError("udp down")),
        ),
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
    ):
        with pytest.raises(RuntimeError, match="udp down"):
            await mgr.rediscover()

    assert len(mgr.switches) == 1
    assert mgr.switches[0].preferred_label == "Desk lamp"
    assert mgr.last_discovery_source == "cache"


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
        [("192.168.1.50", "Desk lamp", cfg, False)],
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
        [("192.168.1.50", "Old plug name", cfg, False)],
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


@pytest.mark.asyncio
async def test_cache_reconnect_attaches_credentials_only_for_klap_hosts(
    tmp_path,
) -> None:
    """Anonymous hosts must not receive account credentials on cache reconnect."""

    from kasa.credentials import Credentials

    db = tmp_path / "cached.sqlite"
    anon_cfg = {
        "host": "192.168.1.10",
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }
    klap_cfg = {
        "host": "192.168.1.20",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Legacy", anon_cfg, False),
            ("192.168.1.20", "Tapo", klap_cfg, True),
        ],
    )

    creds = Credentials(username="alice@example.com", password="hunter2")
    connect_calls: list[Credentials | None] = []

    async def _fake_connect(cfg, *, credentials, timeout):
        del timeout
        connect_calls.append(credentials)
        mock_dev = MagicMock()
        mock_dev.host = cfg.host
        mock_dev.alias = cfg.host
        mock_dev.is_on = False
        mock_dev.config.to_dict_control_credentials.return_value = {
            "host": cfg.host,
            "timeout": 5,
            "connection_type": {
                "device_family": "IOT.SMARTPLUGSWITCH",
                "encryption_type": "XOR",
                "https": False,
            },
        }
        mock_dev.update = AsyncMock()
        mock_dev.disconnect = AsyncMock()
        return mock_dev

    mgr = KasaDeviceManager(discovery_cache_path=db, credentials=creds)
    with patch(
        "app.kasa_device_manager._connect_from_saved_config",
        side_effect=_fake_connect,
    ):
        await mgr.fetch()

    assert mgr.last_discovery_source == "cache"
    assert connect_calls == [None, creds]
    assert mgr.hosts_requiring_klap_auth == ("192.168.1.20",)


@pytest.mark.asyncio
async def test_cache_update_uses_ingest_recovery_instead_of_invalidating(
    tmp_path,
) -> None:
    """A recoverable update failure on cache reconnect must not force UDP rediscovery."""

    db = tmp_path / "cached.sqlite"
    cfg = {
        "host": "192.168.1.50",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    device_discovery_store.save_configs(
        db,
        [("192.168.1.50", "Tapo", cfg, False)],
    )

    mock_dev = MagicMock()
    mock_dev.host = "192.168.1.50"
    mock_dev.alias = "Tapo"
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock(side_effect=_ConnectionError("transient"))
    mock_dev.disconnect = AsyncMock()

    recovered = MagicMock()
    recovered.host = "192.168.1.50"
    recovered.alias = "Tapo recovered"
    recovered.is_on = False
    recovered.config = mock_dev.config
    recovered.config.to_dict_control_credentials = MagicMock(return_value=cfg)

    mock_discover = AsyncMock(return_value={})
    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch(
            "app.kasa_device_manager._connect_smart_plain_http",
            AsyncMock(return_value=recovered),
        ),
        patch("app.kasa_device_manager.Discover.discover", mock_discover),
    ):
        await mgr.fetch()

    assert mgr.last_discovery_source == "cache"
    assert len(mgr.switches) == 1
    assert mgr.switches[0].preferred_label == "Tapo recovered"
    mock_discover.assert_not_awaited()


@pytest.mark.asyncio
async def test_cache_skips_klap_hosts_quietly_without_credentials(tmp_path) -> None:
    """Known KLAP-auth hosts are ignored when no credentials are configured."""

    db = tmp_path / "cached.sqlite"
    anon_cfg = {
        "host": "192.168.1.10",
        "timeout": 5,
        "connection_type": {
            "device_family": "IOT.SMARTPLUGSWITCH",
            "encryption_type": "XOR",
            "https": False,
        },
    }
    klap_cfg = {
        "host": "192.168.1.20",
        "timeout": 5,
        "connection_type": {
            "device_family": "SMART.TAPOPLUG",
            "encryption_type": "KLAP",
            "https": False,
        },
    }
    device_discovery_store.save_configs(
        db,
        [
            ("192.168.1.10", "Legacy", anon_cfg, False),
            ("192.168.1.20", "Tapo", klap_cfg, True),
        ],
    )

    connect_hosts: list[str] = []

    async def _fake_connect(cfg, *, credentials, timeout):
        del credentials, timeout
        connect_hosts.append(cfg.host)
        mock_dev = MagicMock()
        mock_dev.host = cfg.host
        mock_dev.alias = "Legacy"
        mock_dev.is_on = False
        mock_dev.config.to_dict_control_credentials.return_value = anon_cfg
        mock_dev.update = AsyncMock()
        mock_dev.disconnect = AsyncMock()
        return mock_dev

    mgr = KasaDeviceManager(discovery_cache_path=db)
    with patch(
        "app.kasa_device_manager._connect_from_saved_config",
        side_effect=_fake_connect,
    ):
        await mgr.fetch()

    assert mgr.last_discovery_source == "cache"
    assert connect_hosts == ["192.168.1.10"]
    assert len(mgr.switches) == 1
    assert mgr.skipped_auth_hosts == ("192.168.1.20",)
    assert mgr.hosts_requiring_klap_auth == ("192.168.1.20",)
    # Skipped KLAP rows must remain in the discovery cache for the next boot.
    cached = device_discovery_store.load_cached_configs(db)
    hosts = {row[0]: row[3] for row in cached}
    assert hosts == {"192.168.1.10": False, "192.168.1.20": True}


@pytest.mark.asyncio
async def test_rediscover_keeps_switches_visible_during_udp_sweep(tmp_path) -> None:
    """``rediscover`` must not empty ``switches`` while UDP discovery is in flight."""

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
        [("192.168.1.50", "Desk lamp", cfg, False)],
    )

    mock_dev = MagicMock()
    mock_dev.host = "192.168.1.50"
    mock_dev.alias = "Desk lamp"
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock()
    mock_dev.disconnect = AsyncMock()

    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={}),
        ),
    ):
        await mgr.fetch()

    assert len(mgr.switches) == 1

    async def _discover_and_assert_prior_devices(*_args, **_kwargs):
        assert len(mgr.switches) == 1
        return {}

    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(side_effect=_discover_and_assert_prior_devices),
    ):
        await mgr.rediscover()

    assert mgr.switches == ()


@pytest.mark.asyncio
async def test_rediscover_clears_map_when_udp_and_cache_both_fail(tmp_path) -> None:
    """Failed rediscover must not leave disconnected devices in the lookup map."""

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
        [("192.168.1.50", "Desk lamp", cfg, False)],
    )

    mock_dev = MagicMock()
    mock_dev.host = "192.168.1.50"
    mock_dev.alias = "Desk lamp"
    mock_dev.is_on = False
    mock_dev.config = DeviceConfig.from_dict(cfg)
    mock_dev.config.to_dict_control_credentials = MagicMock(return_value=cfg)
    mock_dev.update = AsyncMock()
    mock_dev.disconnect = AsyncMock()

    mgr = KasaDeviceManager(discovery_cache_path=db)
    with (
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(return_value=mock_dev),
        ),
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={}),
        ),
    ):
        await mgr.fetch()

    assert len(mgr.switches) == 1

    with (
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(side_effect=RuntimeError("udp down")),
        ),
        patch(
            "app.kasa_device_manager._connect_from_saved_config",
            AsyncMock(side_effect=RuntimeError("cache down")),
        ),
    ):
        with pytest.raises(RuntimeError, match="udp down"):
            await mgr.rediscover()

    with pytest.raises(NotInitializedError):
        _ = mgr.switches
