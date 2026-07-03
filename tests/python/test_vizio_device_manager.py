"""Unit tests for :mod:`app.vizio_device_manager`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import device_discovery_store
from app.vizio_credentials import migrate_vizio_auth_token_host_to_mac
from app.vizio_device_manager import (
    VizioDeviceManager,
    VizioTvDevice,
    VizioTvEndpoint,
)
from app.vizio_smartcast_client import (
    VizioSmartCastAuthError,
    VizioSmartCastConnectionError,
)


def _tv(*, is_on: bool = False) -> VizioTvDevice:
    endpoint = VizioTvEndpoint(host="192.168.86.201", port=7345)
    device = VizioTvDevice(endpoint, MagicMock(), display_name="Kitchen TV")
    device.set_power(is_on)
    return device


@pytest.mark.asyncio
async def test_fetch_relocates_cached_tv_when_dhcp_ip_changes(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    endpoint = VizioTvEndpoint(
        host="192.168.86.55",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
    )
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    fake_tv = VizioTvDevice(endpoint, fake_client, display_name="Kitchen TV")
    fake_tv.set_power(False)
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch(
            "app.vizio_device_manager.resolve_vizio_tv_ip",
            new_callable=AsyncMock,
            return_value="192.168.86.55",
        ),
        patch.object(
            VizioDeviceManager,
            "_connect_endpoint",
            new_callable=AsyncMock,
            return_value=fake_tv,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await mgr.fetch()
    assert mgr.tvs[0].identifier == "00:bd:3e:d5:f0:11"
    assert mgr.tvs[0].endpoint.host == "192.168.86.55"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_fetch_keeps_unreachable_cached_tv_as_off(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac=None,
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            VizioDeviceManager,
            "_connect_endpoint",
            new_callable=AsyncMock,
            side_effect=VizioSmartCastConnectionError("timeout"),
        ),
        patch(
            "app.vizio_device_manager.lookup_mac_via_arp",
            return_value="00:bd:3e:d5:f0:11",
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await mgr.fetch()
    assert len(mgr.tvs) == 1
    assert mgr.tvs[0].preferred_label == "Kitchen TV"
    assert mgr.tvs[0].ui_power_state() == "off"
    assert mgr.tvs[0].identifier == "00:bd:3e:d5:f0:11"
    rows = device_discovery_store.load_vizio_tvs(db)
    assert rows[0][4] == "00:bd:3e:d5:f0:11"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_fetch_resolves_mac_from_smartcast_and_migrates_host_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac=None,
        diid=None,
    )
    from app.db.secrets import save_vizio_auth_token_to_db, vizio_auth_token_stored_in_db

    save_vizio_auth_token_to_db(
        db,
        mac=None,
        host="192.168.86.201",
        token="legacy-host-token",
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token=None,
        env_auth_token=None,
    )
    endpoint = VizioTvEndpoint(
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
    )
    fake_client = MagicMock()
    fake_client.aclose = AsyncMock()
    fake_tv = VizioTvDevice(endpoint, fake_client, display_name="Kitchen TV", mac="00:bd:3e:d5:f0:11")
    fake_tv.set_power(False)
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            VizioDeviceManager,
            "_connect_endpoint",
            new_callable=AsyncMock,
            return_value=fake_tv,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await mgr.fetch()
    rows = device_discovery_store.load_vizio_tvs(db)
    assert rows[0][4] == "00:bd:3e:d5:f0:11"
    assert vizio_auth_token_stored_in_db(db, mac="00:bd:3e:d5:f0:11", host=None)
    assert not vizio_auth_token_stored_in_db(db, mac=None, host="192.168.86.201")
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_keeps_offline_cached_tv_when_ssdp_finds_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    ssdp = AsyncMock(return_value=[])
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            ssdp,
        ),
    ):
        await mgr.fetch()
        assert len(mgr.tvs) == 1
        assert mgr.tvs[0].identifier == "00:bd:3e:d5:f0:11"
        assert mgr.tvs[0].ui_power_state() == "off"
        await mgr.rediscover()
        assert ssdp.await_count == 1
    assert len(mgr.tvs) == 1
    assert mgr.tvs[0].identifier == "00:bd:3e:d5:f0:11"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_fetch_skips_deviceinfo_when_smartcast_port_closed(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    connect = AsyncMock()
    with (
        patch.object(
            VizioDeviceManager,
            "_connect_endpoint",
            connect,
        ),
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            new_callable=AsyncMock,
            return_value=[],
        ),
    ):
        await mgr.fetch()
    connect.assert_not_called()
    assert len(mgr.tvs) == 1
    assert mgr.tvs[0].ui_power_state() == "off"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_fetch_skips_ssdp_when_cached_mac_tv_is_offline(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    ssdp = AsyncMock(return_value=[])
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            ssdp,
        ),
    ):
        await mgr.fetch()
    ssdp.assert_not_called()
    assert len(mgr.tvs) == 1
    assert mgr.last_discovery_source == "cache"
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_rediscover_runs_fetch_again(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac="00:bd:3e:d5:f0:11",
        diid=None,
    )
    mgr = VizioDeviceManager(
        configured_hosts=[("192.168.86.201", 7345)],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    fake_tv = _tv(is_on=False)
    fake_tv._client.aclose = AsyncMock()  # noqa: SLF001
    ssdp = AsyncMock(return_value=[])
    with (
        patch.object(
            VizioDeviceManager,
            "_smartcast_port_open",
            new_callable=AsyncMock,
            return_value=True,
        ),
        patch.object(
            VizioDeviceManager,
            "_connect_endpoint",
            new_callable=AsyncMock,
            return_value=fake_tv,
        ),
        patch(
            "app.vizio_device_manager.discover_vizio_hosts_ssdp",
            ssdp,
        ),
    ):
        await mgr.fetch()
        assert ssdp.await_count == 0
        await mgr.rediscover()
        assert ssdp.await_count == 1
    assert len(mgr.tvs) == 1
    assert mgr.last_discovery_source == "discovery"
    await mgr.disconnect()


def test_migrate_vizio_auth_token_host_to_mac_skips_on_decrypt_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.secrets import SecretsDecryptError

    db = tmp_path / "cache.sqlite"
    monkeypatch.setattr(
        "app.vizio_credentials.load_vizio_auth_token_from_db",
        MagicMock(side_effect=SecretsDecryptError("bad key")),
    )
    migrate_vizio_auth_token_host_to_mac(
        db,
        host="192.168.86.201",
        mac="00:bd:3e:d5:f0:11",
    )


@pytest.mark.asyncio
async def test_refresh_power_state_marks_off_when_unreachable() -> None:
    tv = _tv(is_on=True)
    tv._client.fetch_tv_active_state = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_refresh_power_state_uses_cast_activity_when_panel_off() -> None:
    tv = _tv(is_on=False)
    tv._client.fetch_tv_active_state = AsyncMock(return_value=True)  # noqa: SLF001
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "on"
    assert tv.is_on is True


@pytest.mark.asyncio
async def test_refresh_power_state_marks_unknown_on_auth_error() -> None:
    tv = _tv(is_on=True)
    tv._client.fetch_tv_active_state = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastAuthError("rejected token")
    )
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "unknown"
    assert tv.is_on is True


@pytest.mark.asyncio
async def test_refresh_power_state_clears_unknown_after_success() -> None:
    tv = _tv(is_on=False)
    tv._power_unknown = True  # noqa: SLF001
    tv._client.fetch_tv_active_state = AsyncMock(return_value=True)  # noqa: SLF001
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "on"


@pytest.mark.asyncio
async def test_offline_tv_registers_off(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    mgr = VizioDeviceManager(
        configured_hosts=[],
        discovery_cache_path=db,
        cli_auth_token="test-token",
    )
    mgr._session = MagicMock()  # noqa: SLF001
    endpoint = VizioTvEndpoint(
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        mac="00:bd:3e:d5:f0:11",
    )
    with patch.object(
        VizioDeviceManager,
        "_connect_endpoint",
        new_callable=AsyncMock,
    ) as connect:
        tv = await mgr._offline_tv(endpoint, "test-token")
    connect.assert_not_called()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_refresh_power_state_poll_forwards_flag_to_client() -> None:
    tv = _tv(is_on=False)
    tv._client.fetch_tv_active_state = AsyncMock(return_value=True)  # noqa: SLF001
    await tv.refresh_power_state(poll=True)
    tv._client.fetch_tv_active_state.assert_awaited_once_with(poll=True)  # noqa: SLF001


@pytest.mark.asyncio
async def test_turn_off_sends_when_power_unknown() -> None:
    tv = _tv(is_on=False)
    tv._power_unknown = True  # noqa: SLF001
    tv._client.power_off = AsyncMock()  # noqa: SLF001
    await tv.turn_off()
    tv._client.power_off.assert_awaited_once_with()  # noqa: SLF001
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_turn_off_skips_smartcast_when_already_off() -> None:
    tv = _tv(is_on=False)
    tv._client.power_off = AsyncMock()  # noqa: SLF001
    await tv.turn_off()
    tv._client.power_off.assert_not_awaited()  # noqa: SLF001
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_turn_off_treats_unreachable_as_off() -> None:
    tv = _tv(is_on=True)
    tv._client.power_off = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    await tv.turn_off()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_turn_on_propagates_connection_error_when_unreachable() -> None:
    tv = _tv()
    tv._client.power_on = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    with pytest.raises(VizioSmartCastConnectionError, match="timeout"):
        await tv.turn_on()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_turn_on_skips_smartcast_when_already_on() -> None:
    tv = _tv(is_on=True)
    tv._client.power_on = AsyncMock()  # noqa: SLF001
    await tv.turn_on()
    tv._client.power_on.assert_not_awaited()  # noqa: SLF001
    assert tv.ui_power_state() == "on"
    assert tv.is_on is True
