"""Unit tests for :mod:`app.vizio_device_manager`."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.vizio_device_manager import VizioTvDevice, VizioTvEndpoint
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
async def test_refresh_power_state_marks_unknown_when_unreachable() -> None:
    tv = _tv(is_on=True)
    tv._client.get_power_on = AsyncMock(  # noqa: SLF001
        side_effect=VizioSmartCastConnectionError("timeout")
    )
    await tv.refresh_power_state()
    assert tv.ui_power_state() == "unknown"
    assert tv.is_on is True


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
