"""Hermetic unit tests for EP1 occupancy tuning alias matching and apply buttons."""

from __future__ import annotations

import asyncio
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioesphomeapi.model import ButtonInfo, NumberInfo, NumberState

from app.device_enums import Ep1OccupancyApplyButton, Ep1OccupancyTuningKind
from app.ep1_calibration import Ep1SettingsTarget
from app.ep1_occupancy_tuning import (
    EP1_OCCUPANCY_TUNING_STATE_INCOMPLETE,
    Ep1OccupancyTuningError,
    Ep1OccupancyTuningSnapshot,
    Ep1OccupancyTuningValidationError,
    _button_entities_by_role,
    _knob_kind_for_number,
    _number_entities_by_kind,
    _snapshot_from_client,
    _validate_knob_in_range,
    _wait_for_number_writes,
    apply_ep1_occupancy_tuning,
)


def _button(*, key: int, name: str, object_id: str) -> ButtonInfo:
    # aioesphomeapi APIModelBase stubs omit kwargs from the type checker.
    info = object.__new__(ButtonInfo)
    object.__setattr__(info, "object_id", object_id)
    object.__setattr__(info, "key", key)
    object.__setattr__(info, "name", name)
    object.__setattr__(info, "disabled_by_default", False)
    object.__setattr__(info, "icon", "")
    object.__setattr__(info, "entity_category", 0)
    object.__setattr__(info, "device_id", 0)
    object.__setattr__(info, "device_class", "")
    return info


def _number(
    *,
    key: int,
    name: str,
    object_id: str,
    min_value: float = 0.0,
    max_value: float = 25.0,
    step: float = 0.1,
    unit: str = "m",
) -> NumberInfo:
    # aioesphomeapi APIModelBase stubs omit kwargs from the type checker.
    info = object.__new__(NumberInfo)
    object.__setattr__(info, "object_id", object_id)
    object.__setattr__(info, "key", key)
    object.__setattr__(info, "name", name)
    object.__setattr__(info, "disabled_by_default", False)
    object.__setattr__(info, "icon", "")
    object.__setattr__(info, "entity_category", 0)
    object.__setattr__(info, "device_id", 0)
    object.__setattr__(info, "min_value", min_value)
    object.__setattr__(info, "max_value", max_value)
    object.__setattr__(info, "step", step)
    object.__setattr__(info, "unit_of_measurement", unit)
    object.__setattr__(info, "mode", 0)
    object.__setattr__(info, "device_class", "")
    return info


def test_knob_kind_aliases_match_live_homey_object_ids() -> None:
    assert (
        _knob_kind_for_number(_number(key=1, name="mmWave Max Distance", object_id="mmwave_max_distance"))
        is Ep1OccupancyTuningKind.MAX_DISTANCE
    )
    assert (
        _knob_kind_for_number(
            _number(
                key=2,
                name="mmWave Minimum Distance",
                object_id="mmwave_minimum_distance",
            )
        )
        is Ep1OccupancyTuningKind.MIN_DISTANCE
    )
    assert (
        _knob_kind_for_number(
            _number(
                key=3,
                name="mmWave Sustain Sensitivity",
                object_id="mmwave_sustain_sensitivity",
                unit="",
                max_value=9.0,
                step=1.0,
            )
        )
        is Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY
    )
    assert (
        _knob_kind_for_number(
            _number(
                key=4,
                name="mmWave sensitivity",
                object_id="mmwave_sensitivity",
                unit="",
                max_value=9.0,
                step=1.0,
            )
        )
        is Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY
    )


def test_number_entities_by_kind_prefers_sen0609_over_sen0395_aliases() -> None:
    entities = [
        _number(
            key=4,
            name="mmWave sensitivity",
            object_id="mmwave_sensitivity",
            max_value=9.0,
            step=1.0,
            unit="",
        ),
        _number(
            key=3,
            name="mmWave Sustain Sensitivity",
            object_id="mmwave_sustain_sensitivity",
            max_value=9.0,
            step=1.0,
            unit="",
        ),
        _number(key=1, name="mmWave Distance", object_id="mmwave_distance"),
        _number(key=2, name="mmWave Max Distance", object_id="mmwave_max_distance"),
    ]
    by_kind = _number_entities_by_kind(entities)
    assert by_kind[Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY].key == 3
    assert by_kind[Ep1OccupancyTuningKind.MAX_DISTANCE].key == 2


def test_number_entities_by_kind_sen0395_subset() -> None:
    entities = [
        _number(
            key=1,
            name="mmWave on latency",
            object_id="mmwave_on_latency",
            max_value=2.0,
            step=0.25,
            unit="seconds",
        ),
        _number(
            key=2,
            name="mmWave off latency",
            object_id="mmwave_off_latency",
            min_value=1.0,
            max_value=600.0,
            step=5.0,
            unit="seconds",
        ),
        _number(
            key=3,
            name="mmWave sensitivity",
            object_id="mmwave_sensitivity",
            max_value=9.0,
            step=1.0,
            unit="",
        ),
    ]
    by_kind = _number_entities_by_kind(entities)
    assert set(by_kind) == {
        Ep1OccupancyTuningKind.ON_LATENCY,
        Ep1OccupancyTuningKind.OFF_LATENCY,
        Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
    }
    assert Ep1OccupancyTuningKind.MAX_DISTANCE not in by_kind
    assert Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY not in by_kind


def test_validate_knob_rejects_off_step_value() -> None:
    number = _number(
        key=1,
        name="mmWave Max Distance",
        object_id="mmwave_max_distance",
        min_value=0.0,
        max_value=25.0,
        step=0.1,
    )
    with pytest.raises(Ep1OccupancyTuningValidationError, match="aligned to step"):
        _validate_knob_in_range(kind=Ep1OccupancyTuningKind.MAX_DISTANCE, number=number, value=8.05)


def test_button_entities_by_role_set_distance_and_sensitivity() -> None:
    buttons = _button_entities_by_role(
        [
            _button(key=10, name="Set Distance", object_id="set_distance"),
            _button(key=11, name="Set Sensitivity", object_id="set_sensitivity"),
            _button(key=12, name="Factory Reset mmWave", object_id="factory_reset_mmwave"),
        ]
    )
    assert set(buttons) == {
        Ep1OccupancyApplyButton.SET_DISTANCE,
        Ep1OccupancyApplyButton.SET_SENSITIVITY,
    }


@pytest.mark.asyncio
async def test_apply_post_write_snapshot_is_seed_only() -> None:
    """Apply must not re-subscribe after the wait's one-shot ESPHome dump."""

    target = Ep1SettingsTarget(
        device_id="28:05:a5:28:c8:48",
        display_label="EP1 (28:05:a5:28:c8:48)",
        display_name="EP1",
        host="192.168.86.214",
        port=6053,
    )
    max_number = _number(key=100, name="mmWave Max Distance", object_id="mmwave_max_distance")
    set_distance = _button(key=200, name="Set Distance", object_id="set_distance")
    entities = [max_number, set_distance]

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=(entities, []))
    client.number_command = MagicMock()
    client.button_command = MagicMock()
    client.subscribe_states = MagicMock()

    soft_snapshot = Ep1OccupancyTuningSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        knobs={},
        port=target.port,
    )

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        patch(
            "app.ep1_occupancy_tuning._wait_for_number_writes",
            new_callable=AsyncMock,
            return_value=(True, {100: MagicMock()}),
        ),
        patch(
            "app.ep1_occupancy_tuning._snapshot_from_client",
            new_callable=AsyncMock,
            return_value=soft_snapshot,
        ) as snap_mock,
        patch("app.ep1_occupancy_tuning.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await apply_ep1_occupancy_tuning(
            device_id=target.device_id,
            max_distance=8.0,
        )

    assert result.knobs_confirmed is True
    assert result.distance_applied is True
    snap_mock.assert_awaited_once()
    assert snap_mock.await_args is not None
    assert snap_mock.await_args.kwargs["collect_missing"] is False
    assert snap_mock.await_args.kwargs["require_complete"] is False
    assert 100 in snap_mock.await_args.kwargs["seed_states"]


@pytest.mark.asyncio
async def test_apply_presses_set_distance_for_max_distance_write() -> None:
    target = Ep1SettingsTarget(
        device_id="28:05:a5:28:c8:48",
        display_label="EP1 (28:05:a5:28:c8:48)",
        display_name="EP1",
        host="192.168.86.214",
        port=6053,
    )
    max_number = _number(key=100, name="mmWave Max Distance", object_id="mmwave_max_distance")
    set_distance = _button(key=200, name="Set Distance", object_id="set_distance")
    entities = [max_number, set_distance]

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=(entities, []))
    client.number_command = MagicMock()
    client.button_command = MagicMock()
    client.subscribe_states = MagicMock()

    snapshot = Ep1OccupancyTuningSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        knobs={},
        port=target.port,
    )

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        patch(
            "app.ep1_occupancy_tuning._wait_for_number_writes",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ),
        patch(
            "app.ep1_occupancy_tuning._snapshot_from_client",
            new_callable=AsyncMock,
            return_value=snapshot,
        ),
        patch("app.ep1_occupancy_tuning.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await apply_ep1_occupancy_tuning(
            device_id=target.device_id,
            max_distance=8.0,
        )

    client.number_command.assert_called_once_with(100, 8.0)
    client.button_command.assert_called_once_with(200)
    assert result.distance_applied is True
    assert result.sensitivity_applied is False


@pytest.mark.asyncio
async def test_apply_requires_set_distance_button_for_distance_write() -> None:
    target = Ep1SettingsTarget(
        device_id="28:05:a5:28:c8:48",
        display_label="EP1 (28:05:a5:28:c8:48)",
        display_name="EP1",
        host="192.168.86.214",
        port=6053,
    )
    max_number = _number(key=100, name="mmWave Max Distance", object_id="mmwave_max_distance")
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([max_number], []))

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        pytest.raises(Ep1OccupancyTuningValidationError, match="set_distance"),
    ):
        await apply_ep1_occupancy_tuning(device_id=target.device_id, max_distance=8.0)


@pytest.mark.asyncio
async def test_apply_requires_set_sensitivity_when_sen0609_and_sen0395_aliases_coexist() -> None:
    target = Ep1SettingsTarget(
        device_id="28:05:a5:28:c8:48",
        display_label="EP1 (28:05:a5:28:c8:48)",
        display_name="EP1",
        host="192.168.86.214",
        port=6053,
    )
    # SEN0395-style alias listed first; SEN0609 sustain still present on the device.
    entities = [
        _number(
            key=4,
            name="mmWave sensitivity",
            object_id="mmwave_sensitivity",
            max_value=9.0,
            step=1.0,
            unit="",
        ),
        _number(
            key=3,
            name="mmWave Sustain Sensitivity",
            object_id="mmwave_sustain_sensitivity",
            max_value=9.0,
            step=1.0,
            unit="",
        ),
    ]
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=(entities, []))

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        pytest.raises(Ep1OccupancyTuningValidationError, match="set_sensitivity"),
    ):
        await apply_ep1_occupancy_tuning(device_id=target.device_id, sustain_sensitivity=4.0)


@pytest.mark.asyncio
async def test_apply_requires_set_sensitivity_for_sen0609_sustain() -> None:
    target = Ep1SettingsTarget(
        device_id="28:05:a5:28:c8:48",
        display_label="EP1 (28:05:a5:28:c8:48)",
        display_name="EP1",
        host="192.168.86.214",
        port=6053,
    )
    sustain = _number(
        key=30,
        name="mmWave Sustain Sensitivity",
        object_id="mmwave_sustain_sensitivity",
        max_value=9.0,
        step=1.0,
        unit="",
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([sustain], []))

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        pytest.raises(Ep1OccupancyTuningValidationError, match="set_sensitivity"),
    ):
        await apply_ep1_occupancy_tuning(device_id=target.device_id, sustain_sensitivity=4.0)


@pytest.mark.asyncio
async def test_apply_sen0395_max_distance_without_set_distance_button() -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:01",
        display_label="EP1 (aa:bb:cc:dd:ee:01)",
        display_name="EP1",
        host="192.168.1.10",
        port=6053,
    )
    max_distance = _number(
        key=40,
        name="mmWave Distance",
        object_id="mmwave_distance",
        max_value=8.0,
        step=0.1,
        unit="m",
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([max_distance], []))
    client.number_command = MagicMock()
    client.button_command = MagicMock()
    client.subscribe_states = MagicMock()

    snapshot = Ep1OccupancyTuningSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        knobs={},
        port=target.port,
    )

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        patch(
            "app.ep1_occupancy_tuning._wait_for_number_writes",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ),
        patch(
            "app.ep1_occupancy_tuning._snapshot_from_client",
            new_callable=AsyncMock,
            return_value=snapshot,
        ),
        patch("app.ep1_occupancy_tuning.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await apply_ep1_occupancy_tuning(
            device_id=target.device_id,
            max_distance=6.0,
        )

    client.number_command.assert_called_once_with(40, 6.0)
    client.button_command.assert_not_called()
    assert result.distance_applied is False


@pytest.mark.asyncio
async def test_apply_sen0395_sensitivity_without_set_sensitivity_button() -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:01",
        display_label="EP1 (aa:bb:cc:dd:ee:01)",
        display_name="EP1",
        host="192.168.1.10",
        port=6053,
    )
    sensitivity = _number(
        key=30,
        name="mmWave sensitivity",
        object_id="mmwave_sensitivity",
        max_value=9.0,
        step=1.0,
        unit="",
    )
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([sensitivity], []))
    client.number_command = MagicMock()
    client.button_command = MagicMock()
    client.subscribe_states = MagicMock()

    snapshot = Ep1OccupancyTuningSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        knobs={},
        port=target.port,
    )

    with (
        patch(
            "app.ep1_occupancy_tuning.resolve_ep1_settings_target",
            return_value=target,
        ),
        patch(
            "app.ep1_occupancy_tuning._ep1_api_client",
            return_value=client,
        ),
        patch(
            "app.ep1_occupancy_tuning._resolved_noise_psk",
            return_value=None,
        ),
        patch(
            "app.ep1_occupancy_tuning._wait_for_number_writes",
            new_callable=AsyncMock,
            return_value=(True, {}),
        ),
        patch(
            "app.ep1_occupancy_tuning._snapshot_from_client",
            new_callable=AsyncMock,
            return_value=snapshot,
        ),
        patch("app.ep1_occupancy_tuning.asyncio.sleep", new_callable=AsyncMock),
    ):
        result = await apply_ep1_occupancy_tuning(
            device_id=target.device_id,
            sustain_sensitivity=4.0,
        )

    client.number_command.assert_called_once_with(30, 4.0)
    client.button_command.assert_not_called()
    assert result.sensitivity_applied is False
    assert result.distance_applied is False


@pytest.mark.asyncio
async def test_snapshot_from_client_raises_when_states_incomplete() -> None:
    target = Ep1SettingsTarget(
        device_id="aa:bb:cc:dd:ee:01",
        display_label="EP1 (aa:bb:cc:dd:ee:01)",
        display_name="EP1",
        host="192.168.1.10",
        port=6053,
    )
    entities = [
        _number(key=1, name="mmWave Max Distance", object_id="mmwave_max_distance"),
        _number(key=2, name="mmWave Minimum Distance", object_id="mmwave_minimum_distance"),
    ]
    client = MagicMock()
    with (
        patch(
            "app.ep1_occupancy_tuning._collect_states_async",
            new_callable=AsyncMock,
            return_value={1: MagicMock()},
        ),
        pytest.raises(
            Ep1OccupancyTuningError,
            match=re.escape(
                EP1_OCCUPANCY_TUNING_STATE_INCOMPLETE.format(
                    host=target.host,
                    port=target.port,
                    got=1,
                    expected=2,
                )
            ),
        ),
    ):
        await _snapshot_from_client(client, target=target, entities=entities)


@pytest.mark.asyncio
async def test_wait_for_number_writes_confirms_without_sibling_states() -> None:
    client = MagicMock()
    holder: list[object] = []
    client.subscribe_states = MagicMock(side_effect=lambda cb: holder.append(cb))

    async def _emit() -> None:
        await asyncio.sleep(0)
        cb = holder[0]
        assert callable(cb)
        state = object.__new__(NumberState)
        object.__setattr__(state, "key", 100)
        object.__setattr__(state, "state", 8.0)
        object.__setattr__(state, "missing_state", False)
        cb(state)

    task = asyncio.create_task(_emit())
    confirmed, collected = await _wait_for_number_writes(
        client,
        expected_numbers={100: 8.0},
        also_collect_keys={101},
        timeout_s=0.2,
    )
    await task

    assert confirmed is True
    assert 100 in collected
    assert 101 not in collected


@pytest.mark.asyncio
async def test_wait_for_number_writes_requires_post_write_callback() -> None:
    """Pre-write matching values must not short-circuit confirmation."""

    client = MagicMock()
    callback_holder: list[object] = []

    def _subscribe(cb: object) -> None:
        callback_holder.append(cb)

    client.subscribe_states = MagicMock(side_effect=_subscribe)

    async def _emit_matching() -> None:
        await asyncio.sleep(0)
        cb = callback_holder[0]
        assert callable(cb)
        state = object.__new__(NumberState)
        object.__setattr__(state, "key", 100)
        object.__setattr__(state, "state", 8.0)
        object.__setattr__(state, "missing_state", False)
        cb(state)

    with patch("app.ep1_occupancy_tuning.asyncio.wait_for", wraps=asyncio.wait_for):
        emit_task = asyncio.create_task(_emit_matching())
        confirmed, collected = await _wait_for_number_writes(
            client,
            expected_numbers={100: 8.0},
            host="192.168.1.10",
            port=6053,
            timeout_s=2.0,
        )
        await emit_task

    assert confirmed is True
    assert 100 in collected
    client.subscribe_states.assert_called_once()


@pytest.mark.asyncio
async def test_wait_for_number_writes_soft_timeout_without_callback() -> None:
    client = MagicMock()
    client.subscribe_states = MagicMock()
    confirmed, collected = await _wait_for_number_writes(
        client,
        expected_numbers={100: 8.0},
        host="192.168.1.10",
        port=6053,
        timeout_s=0.05,
    )
    assert confirmed is False
    assert collected == {}
    client.subscribe_states.assert_called_once()
