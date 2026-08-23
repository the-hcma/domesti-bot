"""Hermetic tests for EP1 bluetooth_proxy domain helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioesphomeapi.core import APIConnectionError
from aioesphomeapi.model import SelectInfo, SelectState

from app.device_enums import Ep1BluetoothProxyState
from app.ep1_bluetooth_proxy import (
    EP1_BLUETOOTH_PROXY_ENTITY_MISSING,
    EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS,
    EP1_BLUETOOTH_PROXY_LISTEN_DISCONNECTED,
    EP1_BLUETOOTH_PROXY_NOT_ENABLED_FOR_PROBE,
    EP1_BLUETOOTH_PROXY_READ_FAILED,
    EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED,
    EP1_BLUETOOTH_PROXY_TEST_OK,
    EP1_BLUETOOTH_PROXY_TEST_ZERO_ADS,
    MAX_BLE_LISTEN_DURATION_S,
    Ep1BluetoothProxyError,
    Ep1BluetoothProxyValidationError,
    _format_ble_address,
    _wait_for_ble_listen,
    _wait_for_select_state,
    probe_ep1_bluetooth_proxy,
    read_ep1_bluetooth_proxy,
    set_ep1_bluetooth_proxy,
)
from app.ep1_calibration import Ep1SettingsTarget

_real_sleep = asyncio.sleep


def test_format_ble_address_int_big_endian() -> None:
    assert _format_ble_address(0xDD3602010974) == "dd:36:02:01:09:74"


def test_format_ble_address_string_passthrough() -> None:
    assert _format_ble_address("aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"


@pytest.mark.asyncio
async def test_read_ep1_bluetooth_proxy_returns_snapshot() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 42
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)

    async def _drive_states() -> None:
        while not state_callbacks:
            await _real_sleep(0)
        select_state = MagicMock(spec=SelectState)
        select_state.key = 42
        select_state.state = Ep1BluetoothProxyState.DISABLED.value
        select_state.missing_state = False
        state_callbacks[0](select_state)

    driver = asyncio.create_task(_drive_states())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
    ):
        snapshot = await read_ep1_bluetooth_proxy(device_id=target.device_id)
    await driver

    assert snapshot.available is True
    assert snapshot.state == Ep1BluetoothProxyState.DISABLED
    assert snapshot.options == ("Disabled", "Enabled")
    client.connect.assert_awaited_once_with(login=True)


@pytest.mark.asyncio
async def test_read_ep1_bluetooth_proxy_missing_select() -> None:
    target = _target()
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([], []))

    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
    ):
        snapshot = await read_ep1_bluetooth_proxy(device_id=target.device_id)

    assert snapshot.available is False
    assert snapshot.state is None
    assert snapshot.options == ()


@pytest.mark.asyncio
async def test_set_ep1_bluetooth_proxy_enables_disabled() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 42
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)
    client.select_command = MagicMock()

    async def _drive() -> None:
        while len(state_callbacks) < 1:
            await _real_sleep(0)
        enabled = MagicMock(spec=SelectState)
        enabled.key = 42
        enabled.state = Ep1BluetoothProxyState.ENABLED.value
        enabled.missing_state = False
        state_callbacks[0](enabled)
        while len(state_callbacks) < 2:
            await _real_sleep(0)
        snapshot_state = MagicMock(spec=SelectState)
        snapshot_state.key = 42
        snapshot_state.state = Ep1BluetoothProxyState.ENABLED.value
        snapshot_state.missing_state = False
        state_callbacks[1](snapshot_state)

    driver = asyncio.create_task(_drive())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
        patch("app.ep1_bluetooth_proxy._STATE_CONFIRM_TIMEOUT_S", 0.5),
    ):
        snapshot = await set_ep1_bluetooth_proxy(device_id=target.device_id, enabled=True)
    await driver

    client.select_command.assert_called_once_with(42, Ep1BluetoothProxyState.ENABLED.value)
    assert snapshot.available is True
    assert snapshot.state == Ep1BluetoothProxyState.ENABLED


@pytest.mark.asyncio
async def test_set_ep1_bluetooth_proxy_validation_when_missing() -> None:
    target = _target()
    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([], []))

    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        pytest.raises(Ep1BluetoothProxyValidationError) as exc_info,
    ):
        await set_ep1_bluetooth_proxy(device_id=target.device_id, enabled=True)

    assert EP1_BLUETOOTH_PROXY_ENTITY_MISSING.format(host=target.host, port=target.port) in str(exc_info.value)


@pytest.mark.asyncio
async def test_probe_ep1_bluetooth_proxy_zero_ads_detail() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 7
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []
    ble_unsubscribe = MagicMock()

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)
    client.subscribe_bluetooth_le_raw_advertisements.return_value = ble_unsubscribe

    async def _drive() -> None:
        while not state_callbacks:
            await _real_sleep(0)
        enabled = MagicMock(spec=SelectState)
        enabled.key = 7
        enabled.state = Ep1BluetoothProxyState.ENABLED.value
        enabled.missing_state = False
        state_callbacks[-1](enabled)

    async def _fast_sleep(_duration: float) -> None:
        await _real_sleep(0)

    driver = asyncio.create_task(_drive())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy.asyncio.sleep", side_effect=_fast_sleep),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
        patch("app.ep1_bluetooth_proxy._STATE_CONFIRM_TIMEOUT_S", 0.5),
    ):
        result = await probe_ep1_bluetooth_proxy(
            device_id=target.device_id,
            duration_s=0.01,
            enable_if_needed=False,
        )
    await driver

    assert result.ok is True
    assert result.samples == ()
    assert result.detail == EP1_BLUETOOTH_PROXY_TEST_ZERO_ADS.format(duration_s=0.01)
    ble_unsubscribe.assert_called_once()


@pytest.mark.asyncio
async def test_probe_ep1_bluetooth_proxy_collects_samples() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 7
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []
    ble_callbacks: list[Callable[[Any], None]] = []
    ads_delivered = asyncio.Event()

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)

    def _subscribe_ble(cb: Callable[[Any], None]) -> Callable[[], None]:
        ble_callbacks.append(cb)
        return MagicMock()

    client.subscribe_bluetooth_le_raw_advertisements.side_effect = _subscribe_ble

    async def _drive() -> None:
        while not state_callbacks:
            await _real_sleep(0)
        enabled = MagicMock(spec=SelectState)
        enabled.key = 7
        enabled.state = Ep1BluetoothProxyState.ENABLED.value
        enabled.missing_state = False
        state_callbacks[-1](enabled)
        while not ble_callbacks:
            await _real_sleep(0)
        response = type(
            "Response",
            (),
            {
                "advertisements": [
                    type(
                        "Advertisement",
                        (),
                        {
                            "address": 0xDD3602010974,
                            "address_type": "public",
                            "data": b"\x01" * 10,
                            "rssi": -48,
                        },
                    )()
                ]
            },
        )()
        ble_callbacks[0](response)
        ads_delivered.set()

    async def _listen_sleep(_duration: float) -> None:
        await asyncio.wait_for(ads_delivered.wait(), timeout=2.0)
        await _real_sleep(0)

    driver = asyncio.create_task(_drive())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy.asyncio.sleep", side_effect=_listen_sleep),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
        patch("app.ep1_bluetooth_proxy._STATE_CONFIRM_TIMEOUT_S", 0.5),
    ):
        result = await probe_ep1_bluetooth_proxy(
            device_id=target.device_id,
            duration_s=0.01,
            enable_if_needed=False,
        )
    await driver

    assert result.ok is True
    assert len(result.samples) == 1
    assert result.samples[0].address == "dd:36:02:01:09:74"
    assert result.samples[0].rssi == -48
    assert result.samples[0].known_test_beacon_label == EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS["dd:36:02:01:09:74"]
    assert result.detail == EP1_BLUETOOTH_PROXY_TEST_OK.format(count=1, duration_s=0.01)


@pytest.mark.asyncio
async def test_probe_ep1_bluetooth_proxy_enables_when_disabled() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 7
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []
    ble_unsubscribe = MagicMock()
    initial_state_sent = asyncio.Event()

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)
    client.subscribe_bluetooth_le_raw_advertisements.return_value = ble_unsubscribe
    client.select_command = MagicMock()

    async def _drive() -> None:
        while len(state_callbacks) < 1:
            await _real_sleep(0)
        disabled = MagicMock(spec=SelectState)
        disabled.key = 7
        disabled.state = Ep1BluetoothProxyState.DISABLED.value
        disabled.missing_state = False
        state_callbacks[0](disabled)
        initial_state_sent.set()
        while len(state_callbacks) < 2:
            await _real_sleep(0)
        enabled = MagicMock(spec=SelectState)
        enabled.key = 7
        enabled.state = Ep1BluetoothProxyState.ENABLED.value
        enabled.missing_state = False
        state_callbacks[1](enabled)

    async def _fast_sleep(_duration: float) -> None:
        await _real_sleep(0)

    driver = asyncio.create_task(_drive())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy.asyncio.sleep", side_effect=_fast_sleep),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
        patch("app.ep1_bluetooth_proxy._STATE_CONFIRM_TIMEOUT_S", 0.5),
    ):
        result = await probe_ep1_bluetooth_proxy(
            device_id=target.device_id,
            duration_s=0.01,
            enable_if_needed=True,
        )
    await driver
    await asyncio.wait_for(initial_state_sent.wait(), timeout=2.0)

    client.select_command.assert_called_once_with(7, Ep1BluetoothProxyState.ENABLED.value)
    assert result.ok is True
    assert result.proxy_was_enabled is False
    assert result.proxy_state == Ep1BluetoothProxyState.ENABLED
    assert result.samples == ()
    assert result.detail == EP1_BLUETOOTH_PROXY_TEST_ZERO_ADS.format(duration_s=0.01)
    ble_unsubscribe.assert_called_once()


@pytest.mark.asyncio
async def test_probe_rejects_disabled_when_enable_if_needed_false() -> None:
    target = _target()
    select = MagicMock(spec=SelectInfo)
    select.key = 7
    select.name = "Bluetooth Proxy"
    select.object_id = "bluetooth_proxy"
    select.options = ["Disabled", "Enabled"]

    state_callbacks: list[Callable[[Any], None]] = []

    client = MagicMock()
    client.connect = AsyncMock()
    client.disconnect = AsyncMock()
    client.list_entities_services = AsyncMock(return_value=([select], []))
    client.subscribe_states.side_effect = lambda cb: state_callbacks.append(cb)
    client.subscribe_bluetooth_le_raw_advertisements = MagicMock()

    async def _drive() -> None:
        while not state_callbacks:
            await _real_sleep(0)
        disabled = MagicMock(spec=SelectState)
        disabled.key = 7
        disabled.state = Ep1BluetoothProxyState.DISABLED.value
        disabled.missing_state = False
        state_callbacks[0](disabled)

    driver = asyncio.create_task(_drive())
    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        patch("app.ep1_bluetooth_proxy._STATE_COLLECT_TIMEOUT_S", 0.5),
        pytest.raises(Ep1BluetoothProxyValidationError) as exc_info,
    ):
        await probe_ep1_bluetooth_proxy(
            device_id=target.device_id,
            duration_s=0.01,
            enable_if_needed=False,
        )
    await driver

    assert str(exc_info.value) == EP1_BLUETOOTH_PROXY_NOT_ENABLED_FOR_PROBE.format(
        state=Ep1BluetoothProxyState.DISABLED
    )
    client.subscribe_bluetooth_le_raw_advertisements.assert_not_called()


@pytest.mark.asyncio
async def test_probe_rejects_duration_above_max() -> None:
    with pytest.raises(Ep1BluetoothProxyValidationError) as exc_info:
        await probe_ep1_bluetooth_proxy(
            device_id="aa:bb:cc:dd:ee:ff",
            duration_s=MAX_BLE_LISTEN_DURATION_S + 0.1,
        )
    assert str(exc_info.value) == (
        f"Expected duration_s in (0, {MAX_BLE_LISTEN_DURATION_S}], got {MAX_BLE_LISTEN_DURATION_S + 0.1!r}"
    )


@pytest.mark.asyncio
async def test_read_ep1_bluetooth_proxy_connection_error() -> None:
    target = _target()
    client = MagicMock()
    client.connect = AsyncMock(side_effect=APIConnectionError("refused"))
    client.disconnect = AsyncMock()

    with (
        patch("app.ep1_bluetooth_proxy.resolve_ep1_settings_target", return_value=target),
        patch("app.ep1_bluetooth_proxy._resolved_noise_psk", return_value=None),
        patch("app.ep1_bluetooth_proxy._ep1_api_client", return_value=client),
        pytest.raises(Ep1BluetoothProxyError) as exc_info,
    ):
        await read_ep1_bluetooth_proxy(device_id=target.device_id)

    assert str(exc_info.value) == EP1_BLUETOOTH_PROXY_READ_FAILED.format(
        host=target.host, port=target.port, exc="refused"
    )


@pytest.mark.asyncio
async def test_wait_for_ble_listen_raises_on_disconnect() -> None:
    disconnected = asyncio.Event()
    disconnected.set()
    with pytest.raises(Ep1BluetoothProxyError) as exc_info:
        await _wait_for_ble_listen(
            duration_s=10.0,
            disconnected=disconnected,
            host="192.168.86.214",
            port=6053,
        )
    assert str(exc_info.value) == EP1_BLUETOOTH_PROXY_LISTEN_DISCONNECTED.format(
        host="192.168.86.214",
        port=6053,
    )


@pytest.mark.asyncio
async def test_wait_for_select_state_timeout_raises() -> None:
    client = MagicMock()
    client.subscribe_states = MagicMock()
    expected = Ep1BluetoothProxyState.ENABLED.value
    with pytest.raises(Ep1BluetoothProxyError) as exc_info:
        await _wait_for_select_state(
            client,
            key=7,
            expected=expected,
            host="192.168.86.214",
            port=6053,
            timeout_s=0.01,
        )
    assert str(exc_info.value) == EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED.format(
        expected=expected,
        host="192.168.86.214",
        port=6053,
    )


def _target(*, device_id: str = "28:05:a5:28:c8:48") -> Ep1SettingsTarget:
    return Ep1SettingsTarget(
        device_id=device_id,
        display_label=f"Everything Presence One ({device_id})",
        display_name="Everything Presence One",
        host="192.168.86.214",
        port=6053,
    )
