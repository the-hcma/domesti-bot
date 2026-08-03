"""Hermetic tests for the ``read-ep1`` REPL command."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domesti_bot_cli import (
    EP1_NOT_LOADED_MSG,
    EP1_READ_FAILED_PREFIX,
    _repl_cmd_read_ep1,
    _Theme,
)
from app.ep1_device_manager import Ep1Device


def _device(*, identifier: str = "aa:bb:cc:dd:ee:01", label: str = "Office EP1") -> Ep1Device:
    device = Ep1Device(
        identifier,
        display_name=label,
        host="192.0.2.10",
        port=6053,
        mac_address=identifier,
    )
    device.apply_entity_state(
        occupancy=True,
        temperature_c=21.5,
        humidity_pct=48.0,
        illuminance_lx=12.0,
    )
    return device


@pytest.mark.asyncio
async def test_read_ep1_refreshes_all_sensors(capsys: pytest.CaptureFixture[str]) -> None:
    device = _device()
    mgr = MagicMock()
    mgr.devices = [device]
    mgr.refresh_device_readings = AsyncMock()

    await _repl_cmd_read_ep1("", ep1_mgr=mgr, theme=_Theme(enabled=False))

    mgr.refresh_device_readings.assert_awaited_once_with(device.identifier)
    out = capsys.readouterr().out
    assert "Office EP1" in out
    assert "occupancy=occupied" in out
    assert "temp_c=21.5" in out


@pytest.mark.asyncio
async def test_read_ep1_resolves_by_label(capsys: pytest.CaptureFixture[str]) -> None:
    office = _device(identifier="aa:bb:cc:dd:ee:01", label="Office EP1")
    hall = _device(identifier="aa:bb:cc:dd:ee:02", label="Hall EP1")
    mgr = MagicMock()
    mgr.devices = [office, hall]
    mgr.refresh_device_readings = AsyncMock()

    await _repl_cmd_read_ep1("Hall", ep1_mgr=mgr, theme=_Theme(enabled=False))

    mgr.refresh_device_readings.assert_awaited_once_with(hall.identifier)
    out = capsys.readouterr().out
    assert "Hall EP1" in out
    assert "Office EP1" not in out


@pytest.mark.asyncio
async def test_read_ep1_reports_when_manager_missing(capsys: pytest.CaptureFixture[str]) -> None:
    await _repl_cmd_read_ep1("", ep1_mgr=None, theme=_Theme(enabled=False))
    err = capsys.readouterr().err
    assert EP1_NOT_LOADED_MSG in err


@pytest.mark.asyncio
async def test_read_ep1_surfaces_refresh_failure(capsys: pytest.CaptureFixture[str]) -> None:
    device = _device()
    mgr = MagicMock()
    mgr.devices = [device]
    mgr.refresh_device_readings = AsyncMock(side_effect=RuntimeError("connect refused"))

    await _repl_cmd_read_ep1("", ep1_mgr=mgr, theme=_Theme(enabled=False))

    err = capsys.readouterr().err
    assert f"{EP1_READ_FAILED_PREFIX}{device.preferred_label}: connect refused" in err


@pytest.mark.asyncio
async def test_ep1_sensor_count_tolerates_uninitialized() -> None:
    from app.device_manager import NotInitializedError
    from app.domesti_bot_cli import _ep1_sensor_count

    mgr: Any = MagicMock()
    type(mgr).devices = property(lambda self: (_ for _ in ()).throw(NotInitializedError("x")))
    assert _ep1_sensor_count(mgr) == 0
    assert _ep1_sensor_count(None) == 0
