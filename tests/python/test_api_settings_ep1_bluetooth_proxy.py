"""Hermetic tests for Settings → EP1 bluetooth_proxy routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.device_enums import Ep1BluetoothProxyState
from app.ep1_bluetooth_proxy import (
    EP1_BLUETOOTH_PROXY_DEVICE_NOT_FOUND,
    EP1_BLUETOOTH_PROXY_ENTITY_MISSING,
    EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS,
    EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED,
    EP1_BLUETOOTH_PROXY_TEST_OK,
    EP1_BLUETOOTH_PROXY_WRITE_FAILED,
    Ep1BleAdvertisementSample,
    Ep1BluetoothProxyError,
    Ep1BluetoothProxyNotFoundError,
    Ep1BluetoothProxySnapshot,
    Ep1BluetoothProxyTestResult,
    Ep1BluetoothProxyValidationError,
)


def test_get_ep1_bluetooth_proxy_returns_snapshot(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    snap = _snapshot()
    with patch(
        "app.api.settings_routes.read_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        return_value=snap,
    ) as read_mock:
        response = client.get("/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["device_id"] == "28:05:a5:28:c8:48"
    assert body["available"] is True
    assert body["state"] == Ep1BluetoothProxyState.DISABLED.value
    assert body["options"] == ["Disabled", "Enabled"]
    read_mock.assert_awaited_once()
    assert read_mock.await_args is not None
    assert read_mock.await_args.kwargs["device_id"] == "28:05:a5:28:c8:48"


def test_get_ep1_bluetooth_proxy_unknown_device_404(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "discovery.sqlite")
    with patch(
        "app.api.settings_routes.read_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        side_effect=Ep1BluetoothProxyNotFoundError(
            EP1_BLUETOOTH_PROXY_DEVICE_NOT_FOUND.format(device_id="aa:bb:cc:dd:ee:ff")
        ),
    ):
        response = client.get("/v1/settings/ep1/devices/aa:bb:cc:dd:ee:ff/bluetooth-proxy")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == EP1_BLUETOOTH_PROXY_DEVICE_NOT_FOUND.format(device_id="aa:bb:cc:dd:ee:ff")


def test_put_ep1_bluetooth_proxy_writes_enabled(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    result = _snapshot(state=Ep1BluetoothProxyState.ENABLED)
    with patch(
        "app.api.settings_routes.set_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        return_value=result,
    ) as set_mock:
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy",
            json={"enabled": True},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["state"] == Ep1BluetoothProxyState.ENABLED.value
    set_mock.assert_awaited_once()
    assert set_mock.await_args is not None
    kwargs = set_mock.await_args.kwargs
    assert kwargs["device_id"] == "28:05:a5:28:c8:48"
    assert kwargs["enabled"] is True


def test_put_ep1_bluetooth_proxy_validation_422(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    detail = EP1_BLUETOOTH_PROXY_ENTITY_MISSING.format(host="192.168.86.214", port=6053)
    with patch(
        "app.api.settings_routes.set_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        side_effect=Ep1BluetoothProxyValidationError(detail),
    ):
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy",
            json={"enabled": True},
        )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == detail


def test_put_ep1_bluetooth_proxy_connection_error_502(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    detail = EP1_BLUETOOTH_PROXY_WRITE_FAILED.format(host="192.168.86.214", port=6053, exc="timeout")
    with patch(
        "app.api.settings_routes.set_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        side_effect=Ep1BluetoothProxyError(detail),
    ):
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy",
            json={"enabled": False},
        )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["detail"] == detail


def test_put_ep1_bluetooth_proxy_select_confirm_timeout_502(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    detail = EP1_BLUETOOTH_PROXY_SELECT_CONFIRM_FAILED.format(
        expected=Ep1BluetoothProxyState.ENABLED.value,
        host="192.168.86.214",
        port=6053,
    )
    with patch(
        "app.api.settings_routes.set_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        side_effect=Ep1BluetoothProxyError(detail),
    ):
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy",
            json={"enabled": True},
        )
    assert response.status_code == HTTPStatus.BAD_GATEWAY
    assert response.json()["detail"] == detail


def test_post_ep1_bluetooth_proxy_test_returns_devices(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    beacon_address = "dd:36:02:01:09:74"
    sample = Ep1BleAdvertisementSample(
        address=beacon_address,
        address_type="public",
        data_length=31,
        known_test_beacon_label=EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS[beacon_address],
        rssi=-55,
    )
    result = Ep1BluetoothProxyTestResult(
        detail=EP1_BLUETOOTH_PROXY_TEST_OK.format(count=1, duration_s=5.0),
        duration_s=5.0,
        ok=True,
        proxy_state=Ep1BluetoothProxyState.ENABLED,
        proxy_was_enabled=False,
        samples=(sample,),
    )
    with patch(
        "app.api.settings_routes.probe_ep1_bluetooth_proxy",
        new_callable=AsyncMock,
        return_value=result,
    ) as test_mock:
        response = client.post(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/bluetooth-proxy/test",
            json={"duration_s": 5.0},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["ok"] is True
    assert body["proxy_was_enabled"] is False
    assert body["proxy_state"] == Ep1BluetoothProxyState.ENABLED.value
    assert len(body["devices"]) == 1
    assert body["devices"][0]["address"] == beacon_address
    assert body["devices"][0]["rssi"] == -55
    assert body["devices"][0]["known_test_beacon_label"] == EP1_BLUETOOTH_PROXY_KNOWN_TEST_BEACONS[beacon_address]
    test_mock.assert_awaited_once()
    assert test_mock.await_args is not None
    assert test_mock.await_args.kwargs["device_id"] == "28:05:a5:28:c8:48"
    assert test_mock.await_args.kwargs["duration_s"] == 5.0


def _client(*, cache_path: Path | None) -> TestClient:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        ep1_noise_psk=None,
        ep1_host=[],
        no_ep1_zeroconf=True,
    )
    return TestClient(create_app(args))


def _snapshot(
    *,
    device_id: str = "28:05:a5:28:c8:48",
    state: Ep1BluetoothProxyState = Ep1BluetoothProxyState.DISABLED,
) -> Ep1BluetoothProxySnapshot:
    return Ep1BluetoothProxySnapshot(
        available=True,
        device_id=device_id,
        display_label=f"Everything Presence One ({device_id})",
        display_name="Everything Presence One",
        host="192.168.86.214",
        options=("Disabled", "Enabled"),
        port=6053,
        state=state,
    )
