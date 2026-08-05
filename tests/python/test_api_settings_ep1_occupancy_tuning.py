"""Hermetic tests for Settings → EP1 occupancy tuning routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.device_enums import Ep1OccupancyTuningKind
from app.ep1_occupancy_tuning import (
    EP1_OCCUPANCY_TUNING_DEVICE_NOT_FOUND,
    Ep1OccupancyTuningField,
    Ep1OccupancyTuningSnapshot,
    Ep1OccupancyTuningValidationError,
)


def test_get_ep1_occupancy_tuning_returns_snapshot(tmp_path: Path) -> None:
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
        "app.api.settings_routes.read_ep1_occupancy_tuning",
        new_callable=AsyncMock,
        return_value=snap,
    ) as read_mock:
        response = client.get("/v1/settings/ep1/devices/28:05:a5:28:c8:48/occupancy-tuning")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["device_id"] == "28:05:a5:28:c8:48"
    assert body["max_distance"]["value"] == 12.0
    assert body["max_distance"]["unit"] == "m"
    assert body["sustain_sensitivity"]["available"] is True
    assert body["knobs_confirmed"] is True
    assert body["distance_applied"] is False
    assert body["sensitivity_applied"] is False
    read_mock.assert_awaited_once()


def test_get_ep1_occupancy_tuning_unknown_device_404(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "discovery.sqlite")
    response = client.get("/v1/settings/ep1/devices/aa:bb:cc:dd:ee:ff/occupancy-tuning")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == EP1_OCCUPANCY_TUNING_DEVICE_NOT_FOUND.format(device_id="aa:bb:cc:dd:ee:ff")


def test_put_ep1_occupancy_tuning_unconfirmed(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    base = _snapshot()
    result = Ep1OccupancyTuningSnapshot(
        device_id=base.device_id,
        display_label=base.display_label,
        display_name=base.display_name,
        host=base.host,
        port=base.port,
        knobs=base.knobs,
        distance_applied=True,
        knobs_confirmed=False,
        sensitivity_applied=False,
    )
    with patch(
        "app.api.settings_routes.apply_ep1_occupancy_tuning",
        new_callable=AsyncMock,
        return_value=result,
    ):
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/occupancy-tuning",
            json={"max_distance": 8},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["knobs_confirmed"] is False


def test_put_ep1_occupancy_tuning_validation_422(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    with patch(
        "app.api.settings_routes.apply_ep1_occupancy_tuning",
        new_callable=AsyncMock,
        side_effect=Ep1OccupancyTuningValidationError("Expected max_distance in [0.0, 25.0], got 99"),
    ):
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/occupancy-tuning",
            json={"max_distance": 99},
        )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert "max_distance" in response.json()["detail"]


def test_put_ep1_occupancy_tuning_writes_knobs(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    knobs = dict(_snapshot().knobs)
    knobs[Ep1OccupancyTuningKind.MAX_DISTANCE] = _knob_field(
        Ep1OccupancyTuningKind.MAX_DISTANCE,
        value=8.0,
        unit="m",
    )
    base = _snapshot()
    result = Ep1OccupancyTuningSnapshot(
        device_id=base.device_id,
        display_label=base.display_label,
        display_name=base.display_name,
        host=base.host,
        port=base.port,
        knobs=knobs,
        distance_applied=True,
        knobs_confirmed=True,
        sensitivity_applied=False,
    )
    with patch(
        "app.api.settings_routes.apply_ep1_occupancy_tuning",
        new_callable=AsyncMock,
        return_value=result,
    ) as apply_mock:
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/occupancy-tuning",
            json={"max_distance": 8},
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["max_distance"]["value"] == 8.0
    assert body["distance_applied"] is True
    assert body["knobs_confirmed"] is True
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    kwargs = apply_mock.await_args.kwargs
    assert kwargs["device_id"] == "28:05:a5:28:c8:48"
    assert kwargs["max_distance"] == 8.0


def _client(*, cache_path: Path | None) -> TestClient:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        ep1_noise_psk=None,
        ep1_host=[],
        no_ep1_zeroconf=True,
    )
    return TestClient(create_app(args))


def _knob_field(
    kind: Ep1OccupancyTuningKind,
    *,
    value: float,
    unit: str | None,
    min_value: float = 0.0,
    max_value: float = 25.0,
    step: float = 0.1,
    available: bool = True,
) -> Ep1OccupancyTuningField:
    if not available:
        return Ep1OccupancyTuningField(
            available=False,
            kind=kind,
            max_value=None,
            min_value=None,
            step=None,
            unit=None,
            value=None,
        )
    return Ep1OccupancyTuningField(
        available=True,
        kind=kind,
        max_value=max_value,
        min_value=min_value,
        step=step,
        unit=unit,
        value=value,
    )


def _snapshot(*, device_id: str = "28:05:a5:28:c8:48") -> Ep1OccupancyTuningSnapshot:
    return Ep1OccupancyTuningSnapshot(
        device_id=device_id,
        display_label=f"Everything Presence One ({device_id})",
        display_name="Everything Presence One",
        host="192.168.86.214",
        port=6053,
        knobs={
            Ep1OccupancyTuningKind.MAX_DISTANCE: _knob_field(
                Ep1OccupancyTuningKind.MAX_DISTANCE,
                value=12.0,
                unit="m",
            ),
            Ep1OccupancyTuningKind.MIN_DISTANCE: _knob_field(
                Ep1OccupancyTuningKind.MIN_DISTANCE,
                value=0.6,
                unit="m",
                min_value=0.6,
            ),
            Ep1OccupancyTuningKind.OFF_LATENCY: _knob_field(
                Ep1OccupancyTuningKind.OFF_LATENCY,
                value=15.0,
                unit="seconds",
                min_value=10.0,
                max_value=600.0,
                step=5.0,
            ),
            Ep1OccupancyTuningKind.ON_LATENCY: _knob_field(
                Ep1OccupancyTuningKind.ON_LATENCY,
                value=0.0,
                unit="seconds",
                max_value=2.0,
                step=0.25,
            ),
            Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY: _knob_field(
                Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
                value=7.0,
                unit=None,
                max_value=9.0,
                step=1.0,
            ),
            Ep1OccupancyTuningKind.TRIGGER_DISTANCE: _knob_field(
                Ep1OccupancyTuningKind.TRIGGER_DISTANCE,
                value=6.0,
                unit="m",
            ),
            Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: _knob_field(
                Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
                value=5.0,
                unit=None,
                max_value=9.0,
                step=1.0,
            ),
        },
    )
