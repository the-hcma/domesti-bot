"""Hermetic tests for EP1 calibration write settle helpers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest
from aioesphomeapi.model import NumberState, SensorState

from app.ep1_calibration import (
    Ep1CalibrationWriteSettle,
    _number_state_matches_expected,
    _sensor_reading_changed,
    _wait_for_calibration_write_effects,
)


def test_number_state_matches_expected_tolerance() -> None:
    state = MagicMock(spec=NumberState)
    state.state = 10.0000001
    state.missing_state = False
    assert _number_state_matches_expected(state, 10.0) is True
    state.state = 11.0
    assert _number_state_matches_expected(state, 10.0) is False


def test_sensor_reading_changed_detects_delta() -> None:
    assert _sensor_reading_changed(baseline=9.5, current=19.5) is True
    assert _sensor_reading_changed(baseline=9.5, current=9.5) is False
    assert _sensor_reading_changed(baseline=None, current=1.0) is False
    assert _sensor_reading_changed(baseline=1.0, current=None) is False


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_counts_post_confirm_sensor_refresh() -> None:
    """Sensor update after number confirm (not pre-confirm race) completes the wait."""

    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    async def _drive() -> None:
        while not callbacks:
            await asyncio.sleep(0)
        cb = callbacks[0]
        number = MagicMock(spec=NumberState)
        number.key = 1
        number.state = 10.0
        number.missing_state = False
        cb(number)
        await asyncio.sleep(0)
        fresh = MagicMock(spec=SensorState)
        fresh.key = 2
        fresh.state = 19.5
        fresh.missing_state = False
        cb(fresh)

    driver = asyncio.create_task(_drive())
    assert await _wait_for_calibration_write_effects(
        client,
        expected_numbers={1: 10.0},
        sensor_keys={2},
        sensor_baselines={2: 9.5},
        timeout_s=0.5,
    ) == Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=True)
    await driver


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_ignores_pre_confirm_sensor_delta() -> None:
    """A sensor republish before number confirm must not complete the waiter."""

    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    waiter = asyncio.create_task(
        _wait_for_calibration_write_effects(
            client,
            expected_numbers={1: 10.0},
            sensor_keys={2},
            sensor_baselines={2: 9.5},
            timeout_s=2.0,
        )
    )
    while not callbacks:
        await asyncio.sleep(0)
    cb = callbacks[0]

    # Natural republish before the offset number confirms (still pre-offset).
    raced = MagicMock(spec=SensorState)
    raced.key = 2
    raced.state = 11.0
    raced.missing_state = False
    cb(raced)
    await asyncio.sleep(0)
    assert not waiter.done()

    number = MagicMock(spec=NumberState)
    number.key = 1
    number.state = 10.0
    number.missing_state = False
    # Still matches the pre-write baseline after confirm — must not complete.
    stale = MagicMock(spec=SensorState)
    stale.key = 2
    stale.state = 9.5
    stale.missing_state = False
    cb(number)
    cb(stale)
    await asyncio.sleep(0)
    assert not waiter.done()

    fresh = MagicMock(spec=SensorState)
    fresh.key = 2
    fresh.state = 21.0
    fresh.missing_state = False
    cb(fresh)
    assert await waiter == Ep1CalibrationWriteSettle(
        offsets_confirmed=True,
        readings_refreshed=True,
    )


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_returns_unconfirmed_when_numbers_timeout() -> None:
    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    assert await _wait_for_calibration_write_effects(
        client,
        expected_numbers={1: 10.0},
        sensor_keys={2},
        sensor_baselines={2: 9.5},
        host="192.0.2.1",
        port=6053,
        timeout_s=0.05,
    ) == Ep1CalibrationWriteSettle(offsets_confirmed=False, readings_refreshed=False)


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_skips_sensors_without_baseline() -> None:
    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    async def _drive() -> None:
        while not callbacks:
            await asyncio.sleep(0)
        cb = callbacks[0]
        number = MagicMock(spec=NumberState)
        number.key = 1
        number.state = 10.0
        number.missing_state = False
        cb(number)

    driver = asyncio.create_task(_drive())
    assert await _wait_for_calibration_write_effects(
        client,
        expected_numbers={1: 10.0},
        sensor_keys={2},
        sensor_baselines={2: None},
        timeout_s=0.5,
    ) == Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=False)
    await driver


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_stale_state_keeps_waiter_pending() -> None:
    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    waiter = asyncio.create_task(
        _wait_for_calibration_write_effects(
            client,
            expected_numbers={1: 10.0},
            sensor_keys={2},
            sensor_baselines={2: 9.5},
            timeout_s=2.0,
        )
    )
    while not callbacks:
        await asyncio.sleep(0)
    cb = callbacks[0]

    number = MagicMock(spec=NumberState)
    number.key = 1
    number.state = 10.0
    number.missing_state = False
    stale = MagicMock(spec=SensorState)
    stale.key = 2
    stale.state = 9.5
    stale.missing_state = False
    cb(number)
    cb(stale)
    await asyncio.sleep(0)
    assert not waiter.done()

    fresh = MagicMock(spec=SensorState)
    fresh.key = 2
    fresh.state = 19.5
    fresh.missing_state = False
    cb(fresh)
    assert await waiter == Ep1CalibrationWriteSettle(
        offsets_confirmed=True,
        readings_refreshed=True,
    )


@pytest.mark.asyncio
async def test_wait_for_calibration_write_effects_waits_for_sensor_refresh() -> None:
    callbacks: list[Any] = []
    client = MagicMock()
    client.subscribe_states.side_effect = lambda cb: callbacks.append(cb)

    async def _drive() -> None:
        while not callbacks:
            await asyncio.sleep(0)
        cb = callbacks[0]
        number = MagicMock(spec=NumberState)
        number.key = 1
        number.state = 10.0
        number.missing_state = False
        stale = MagicMock(spec=SensorState)
        stale.key = 2
        stale.state = 9.5
        stale.missing_state = False
        cb(number)
        cb(stale)
        await asyncio.sleep(0)
        fresh = MagicMock(spec=SensorState)
        fresh.key = 2
        fresh.state = 19.5
        fresh.missing_state = False
        cb(fresh)

    driver = asyncio.create_task(_drive())
    assert await _wait_for_calibration_write_effects(
        client,
        expected_numbers={1: 10.0},
        sensor_keys={2},
        sensor_baselines={2: 9.5},
        timeout_s=2.0,
    ) == Ep1CalibrationWriteSettle(offsets_confirmed=True, readings_refreshed=True)
    await driver
