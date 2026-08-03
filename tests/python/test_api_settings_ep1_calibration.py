"""Hermetic tests for Settings → EP1 target devices and calibration offsets."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient

from app import device_discovery_store
from app.api.app import create_app
from app.device_enums import Ep1CalibrationOffsetKind
from app.ep1_calibration import (
    Ep1CalibrationOffsetField,
    Ep1CalibrationSnapshot,
    Ep1SettingsTarget,
    list_ep1_settings_targets,
)
from app.server_runtime import runtime as server_runtime


def _client(*, cache_path: Path | None) -> TestClient:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        ep1_noise_psk=None,
        ep1_host=[],
        no_ep1_zeroconf=True,
    )
    return TestClient(create_app(args))


def _offset_field(
    kind: Ep1CalibrationOffsetKind,
    *,
    value: float,
    reading: float,
    unit: str,
    min_value: float = -50.0,
    max_value: float = 50.0,
    step: float = 1.0,
) -> Ep1CalibrationOffsetField:
    return Ep1CalibrationOffsetField(
        available=True,
        kind=kind,
        max_value=max_value,
        min_value=min_value,
        reading=reading,
        step=step,
        unit=unit,
        value=value,
    )


def _snapshot(*, device_id: str = "28:05:a5:28:c8:48") -> Ep1CalibrationSnapshot:
    return Ep1CalibrationSnapshot(
        device_id=device_id,
        display_label=f"Everything Presence One ({device_id})",
        display_name="Everything Presence One",
        host="192.168.86.214",
        port=6053,
        offsets={
            Ep1CalibrationOffsetKind.HUMIDITY: _offset_field(
                Ep1CalibrationOffsetKind.HUMIDITY,
                value=0.0,
                reading=40.0,
                unit="%",
                step=0.1,
            ),
            Ep1CalibrationOffsetKind.ILLUMINANCE: _offset_field(
                Ep1CalibrationOffsetKind.ILLUMINANCE,
                value=0.0,
                reading=1.0,
                unit="lx",
            ),
            Ep1CalibrationOffsetKind.TEMPERATURE: _offset_field(
                Ep1CalibrationOffsetKind.TEMPERATURE,
                value=0.0,
                reading=22.0,
                unit="°C",
                min_value=-20.0,
                max_value=20.0,
                step=0.1,
            ),
        },
    )


def test_get_ep1_devices_lists_cached_rows(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="Everything Presence One 28c848",
    )
    client = _client(cache_path=db)
    response = client.get("/v1/settings/ep1/devices")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert len(body["devices"]) == 1
    row = body["devices"][0]
    assert row["device_id"] == "28:05:a5:28:c8:48"
    assert row["host"] == "192.168.86.214"
    assert row["port"] == 6053
    assert "28:05:a5:28:c8:48" in row["display_label"]


def test_get_ep1_calibration_returns_snapshot(tmp_path: Path) -> None:
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
        "app.api.settings_routes.read_ep1_calibration",
        new_callable=AsyncMock,
        return_value=snap,
    ) as read_mock:
        response = client.get("/v1/settings/ep1/devices/28:05:a5:28:c8:48/calibration")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["device_id"] == "28:05:a5:28:c8:48"
    assert body["illuminance"]["value"] == 0.0
    assert body["illuminance"]["unit"] == "lx"
    assert body["temperature"]["available"] is True
    assert body["humidity"]["reading"] == 40.0
    read_mock.assert_awaited_once()


def test_put_ep1_calibration_writes_offsets(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.86.214",
        port=6053,
        mac="28:05:a5:28:c8:48",
        friendly_name="EP1",
    )
    client = _client(cache_path=db)
    offsets = dict(_snapshot().offsets)
    offsets[Ep1CalibrationOffsetKind.ILLUMINANCE] = _offset_field(
        Ep1CalibrationOffsetKind.ILLUMINANCE,
        value=7.0,
        reading=8.0,
        unit="lx",
    )
    base = _snapshot()
    result = Ep1CalibrationSnapshot(
        device_id=base.device_id,
        display_label=base.display_label,
        display_name=base.display_name,
        host=base.host,
        port=base.port,
        offsets=offsets,
    )
    with patch(
        "app.api.settings_routes.apply_ep1_calibration_offsets",
        new_callable=AsyncMock,
        return_value=result,
    ) as apply_mock:
        response = client.put(
            "/v1/settings/ep1/devices/28:05:a5:28:c8:48/calibration",
            json={"illuminance_offset": 7},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["illuminance"]["value"] == 7.0
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    kwargs = apply_mock.await_args.kwargs
    assert kwargs["device_id"] == "28:05:a5:28:c8:48"
    assert kwargs["illuminance_offset"] == 7.0


def test_get_ep1_calibration_unknown_device_404(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "discovery.sqlite")
    response = client.get("/v1/settings/ep1/devices/aa:bb:cc:dd:ee:ff/calibration")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "matched device_id" in response.json()["detail"]


def test_list_ep1_settings_targets_from_cache(tmp_path: Path) -> None:
    db = tmp_path / "discovery.sqlite"
    device_discovery_store.upsert_ep1_device(
        db,
        host="192.168.1.10",
        port=6053,
        mac="aa:bb:cc:dd:ee:01",
        friendly_name="Hall",
    )
    server_runtime.device_state = None
    targets = list_ep1_settings_targets(cache_path=db, ep1_mgr=None)
    assert targets == [
        Ep1SettingsTarget(
            device_id="aa:bb:cc:dd:ee:01",
            display_label="Hall (aa:bb:cc:dd:ee:01)",
            display_name="Hall",
            host="192.168.1.10",
            port=6053,
        )
    ]


def test_post_ep1_test_uses_device_id(tmp_path: Path) -> None:
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
        "app.api.settings_routes.probe_ep1_noise_psk",
        new_callable=AsyncMock,
        return_value=MagicMock(ok=True, detail="ok", source=None),
    ) as probe_mock:
        response = client.post(
            "/v1/settings/ep1-noise-psk/test",
            json={"device_id": "28:05:a5:28:c8:48"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["ok"] is True
    assert probe_mock.await_args is not None
    assert probe_mock.await_args.kwargs["device_id"] == "28:05:a5:28:c8:48"
