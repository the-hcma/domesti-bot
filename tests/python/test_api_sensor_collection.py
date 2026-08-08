"""Tests for Automations → Data sensor collection HTTP routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from time import time as wall_time
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.device_enums import DeviceConditionState, SensorChartWindow, SensorCollectionKey
from app.sensor_collection_store import insert_sensor_sample
from app.server_runtime import runtime


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def _install_ep1_state(device_id: str = "aa:bb:cc:dd:ee:ff") -> None:
    device = MagicMock()
    device.identifier = device_id
    device.mac_address = device_id
    device.preferred_label = "Office"
    device.occupancy_state = DeviceConditionState.CLEAR.value
    device.temperature_c = 20.0
    device.humidity_pct = 50.0
    device.illuminance_lx = 80.0
    state = MagicMock()
    state.ep1_mgr = MagicMock()
    state.ep1_mgr.devices = [device]
    runtime.device_state = state


def test_get_sensors_lists_ep1_readings(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _install_ep1_state()
    try:
        response = client.get("/v1/sensor-collection/sensors")
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        keys = {(row["device_id"], row["sensor_key"]) for row in body["sensors"]}
        assert ("aa:bb:cc:dd:ee:ff", SensorCollectionKey.OCCUPANCY.value) in keys
        assert ("aa:bb:cc:dd:ee:ff", SensorCollectionKey.TEMPERATURE_C.value) in keys
        office = next(row for row in body["sensors"] if row["sensor_key"] == SensorCollectionKey.TEMPERATURE_C.value)
        assert "Office" in office["device_display"]
        assert office["enabled"] is False
    finally:
        runtime.device_state = None


def test_put_sensor_enables_collection(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _install_ep1_state()
    try:
        response = client.put(
            f"/v1/sensor-collection/sensors/aa:bb:cc:dd:ee:ff/{SensorCollectionKey.TEMPERATURE_C.value}",
            json={"enabled": True, "interval_s": 15},
        )
        assert response.status_code == HTTPStatus.OK
        body = response.json()
        assert body["enabled"] is True
        assert body["interval_s"] == 15
    finally:
        runtime.device_state = None


def test_get_samples_respects_window(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    device_id = "aa:bb:cc:dd:ee:ff"
    key = SensorCollectionKey.ILLUMINANCE_LX
    t = wall_time()
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=t - 5,
        sensor_key=key,
        unit="lx",
        value=11.0,
    )
    insert_sensor_sample(
        db,
        device_id=device_id,
        family_id="ep1",
        recorded_at=t - 200,
        sensor_key=key,
        unit="lx",
        value=22.0,
    )

    response = client.get(
        "/v1/sensor-collection/samples",
        params={
            "device_id": device_id,
            "sensor_key": key.value,
            "window": SensorChartWindow.LAST_MINUTE.value,
        },
    )
    assert response.status_code == HTTPStatus.OK
    values = [point["value"] for point in response.json()["points"]]
    assert 11.0 in values
    assert 22.0 not in values


def test_put_sensor_rejects_bad_interval(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    response = client.put(
        f"/v1/sensor-collection/sensors/aa:bb:cc:dd:ee:ff/{SensorCollectionKey.OCCUPANCY.value}",
        json={"enabled": True, "interval_s": 9},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_get_and_put_retention(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    get_response = client.get("/v1/sensor-collection/retention")
    assert get_response.status_code == HTTPStatus.OK
    assert get_response.json() == {"max_age_days": 60.0, "unlimited": False}

    put_response = client.put(
        "/v1/sensor-collection/retention",
        json={"max_age_days": 90, "unlimited": False},
    )
    assert put_response.status_code == HTTPStatus.OK
    assert put_response.json() == {"max_age_days": 90.0, "unlimited": False}

    again = client.get("/v1/sensor-collection/retention")
    assert again.json()["max_age_days"] == 90.0


def test_put_retention_unlimited_allows_zero_max_age_days(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/sensor-collection/retention",
        json={"max_age_days": 14, "unlimited": False},
    )
    put_response = client.put(
        "/v1/sensor-collection/retention",
        json={"max_age_days": 0, "unlimited": True},
    )
    assert put_response.status_code == HTTPStatus.OK
    body = put_response.json()
    assert body["unlimited"] is True
    assert body["max_age_days"] == 14.0


def test_put_retention_limited_rejects_zero_max_age_days(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    put_response = client.put(
        "/v1/sensor-collection/retention",
        json={"max_age_days": 0, "unlimited": False},
    )
    assert put_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_put_retention_rejects_non_finite_max_age_days(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    for payload in (
        {"max_age_days": "Infinity", "unlimited": False},
        {"max_age_days": "NaN", "unlimited": True},
    ):
        put_response = client.put("/v1/sensor-collection/retention", json=payload)
        assert put_response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY, payload
