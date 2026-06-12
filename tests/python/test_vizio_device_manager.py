"""Unit tests for :mod:`app.vizio_device_manager`."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import device_discovery_store
from app.vizio_device_manager import VizioDeviceManager, VizioTvDevice, VizioTvEndpoint
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
            "_connect_endpoint",
            new_callable=AsyncMock,
            side_effect=VizioSmartCastConnectionError("timeout"),
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
    await mgr.disconnect()


@pytest.mark.asyncio
async def test_refresh_power_state_marks_off_when_unreachable() -> None:
    tv = _tv(is_on=True)
    tv._client.get_power_on = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False


@pytest.mark.asyncio
async def test_refresh_power_state_marks_unknown_on_auth_error() -> None:
    tv = _tv(is_on=True)
    tv._client.get_power_on = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastAuthError("rejected token")
    )
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "unknown"
    assert tv.is_on is True


@pytest.mark.asyncio
async def test_refresh_power_state_clears_unknown_after_success() -> None:
    tv = _tv(is_on=False)
    tv._power_unknown = True  # noqa: SLF001
    tv._client.get_power_on = AsyncMock(return_value=True)  # noqa: SLF001
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "on"


@pytest.mark.asyncio
async def test_turn_off_treats_unreachable_as_off() -> None:
    tv = _tv(is_on=True)
    tv._client.power_off = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    await tv.turn_off()
    assert tv.ui_power_state() == "off"
    assert tv.is_on is False
