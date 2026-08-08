"""Hermetic tests for Settings → Kasa motion (PIR) tuning routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.device_enums import KasaPirRange
from app.kasa_motion_tuning import (
    KASA_MOTION_TUNING_DEVICE_NOT_FOUND,
    KASA_MOTION_TUNING_THRESHOLD_RANGE,
    KasaMotionSettingsTarget,
    KasaMotionTuningNotFoundError,
    KasaMotionTuningSnapshot,
    KasaMotionTuningValidationError,
)


def test_get_kasa_motion_devices_lists_targets() -> None:
    client = _client()
    rows = [
        KasaMotionSettingsTarget(
            device_id="98:25:4a:64:ac:90",
            display_label="Garage light (98:25:4a:64:ac:90)",
            display_name="Garage light",
            host="192.168.86.186",
            model="KS200M(US)",
        )
    ]
    with patch(
        "app.api.settings_routes.list_kasa_motion_settings_targets",
        return_value=rows,
    ):
        response = client.get("/v1/settings/kasa/devices")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert len(body["devices"]) == 1
    assert body["devices"][0]["device_id"] == "98:25:4a:64:ac:90"
    assert body["devices"][0]["model"] == "KS200M(US)"


def test_get_kasa_motion_tuning_returns_snapshot() -> None:
    client = _client()
    snap = _snapshot()
    with patch(
        "app.api.settings_routes.read_kasa_motion_tuning",
        new_callable=AsyncMock,
        return_value=snap,
    ) as read_mock:
        response = client.get("/v1/settings/kasa/devices/98:25:4a:64:ac:90/motion-tuning")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["device_id"] == "98:25:4a:64:ac:90"
    assert body["pir_enabled"] is True
    assert body["pir_range"] == "Mid"
    assert body["pir_threshold"] == 50
    assert body["ambient_available"] is True
    assert body["ambient_light"] == 64
    assert body["knobs_confirmed"] is True
    read_mock.assert_awaited_once()


def test_get_kasa_motion_tuning_unknown_device_404() -> None:
    client = _client()
    with patch(
        "app.api.settings_routes.read_kasa_motion_tuning",
        new_callable=AsyncMock,
        side_effect=KasaMotionTuningNotFoundError(
            KASA_MOTION_TUNING_DEVICE_NOT_FOUND.format(device_id="aa:bb:cc:dd:ee:ff")
        ),
    ):
        response = client.get("/v1/settings/kasa/devices/aa:bb:cc:dd:ee:ff/motion-tuning")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["detail"] == KASA_MOTION_TUNING_DEVICE_NOT_FOUND.format(device_id="aa:bb:cc:dd:ee:ff")


def test_put_kasa_motion_tuning_writes_knobs() -> None:
    client = _client()
    base = _snapshot()
    result = KasaMotionTuningSnapshot(
        ambient_available=base.ambient_available,
        ambient_light=base.ambient_light,
        ambient_light_enabled=False,
        device_id=base.device_id,
        display_label=base.display_label,
        display_name=base.display_name,
        host=base.host,
        knobs_confirmed=True,
        model=base.model,
        pir_enabled=False,
        pir_percent=base.pir_percent,
        pir_range=KasaPirRange.NEAR,
        pir_range_choices=base.pir_range_choices,
        pir_threshold=40,
        pir_triggered=base.pir_triggered,
    )
    with patch(
        "app.api.settings_routes.apply_kasa_motion_tuning",
        new_callable=AsyncMock,
        return_value=result,
    ) as apply_mock:
        response = client.put(
            "/v1/settings/kasa/devices/98:25:4a:64:ac:90/motion-tuning",
            json={
                "pir_enabled": False,
                "pir_range": "Near",
                "pir_threshold": 40,
                "ambient_light_enabled": False,
            },
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["pir_enabled"] is False
    assert body["pir_range"] == "Near"
    assert body["pir_threshold"] == 40
    assert body["ambient_light_enabled"] is False
    apply_mock.assert_awaited_once()
    assert apply_mock.await_args is not None
    kwargs = apply_mock.await_args.kwargs
    assert kwargs["device_id"] == "98:25:4a:64:ac:90"
    assert kwargs["pir_enabled"] is False
    assert kwargs["pir_range"] is KasaPirRange.NEAR
    assert kwargs["pir_threshold"] == 40
    assert kwargs["ambient_light_enabled"] is False


def test_put_kasa_motion_tuning_validation_422() -> None:
    client = _client()
    detail = KASA_MOTION_TUNING_THRESHOLD_RANGE.format(value=101)
    with patch(
        "app.api.settings_routes.apply_kasa_motion_tuning",
        new_callable=AsyncMock,
        side_effect=KasaMotionTuningValidationError(detail),
    ) as apply_mock:
        response = client.put(
            "/v1/settings/kasa/devices/98:25:4a:64:ac:90/motion-tuning",
            json={"ambient_light_enabled": True},
        )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert response.json()["detail"] == detail
    apply_mock.assert_awaited_once()


def test_put_kasa_motion_tuning_unconfirmed() -> None:
    client = _client()
    base = _snapshot()
    result = KasaMotionTuningSnapshot(
        ambient_available=base.ambient_available,
        ambient_light=base.ambient_light,
        ambient_light_enabled=base.ambient_light_enabled,
        device_id=base.device_id,
        display_label=base.display_label,
        display_name=base.display_name,
        host=base.host,
        knobs_confirmed=False,
        model=base.model,
        pir_enabled=base.pir_enabled,
        pir_percent=base.pir_percent,
        pir_range=base.pir_range,
        pir_range_choices=base.pir_range_choices,
        pir_threshold=base.pir_threshold,
        pir_triggered=base.pir_triggered,
    )
    with patch(
        "app.api.settings_routes.apply_kasa_motion_tuning",
        new_callable=AsyncMock,
        return_value=result,
    ):
        response = client.put(
            "/v1/settings/kasa/devices/98:25:4a:64:ac:90/motion-tuning",
            json={"pir_range": "Near"},
        )
    assert response.status_code == HTTPStatus.OK
    assert response.json()["knobs_confirmed"] is False


def _client() -> TestClient:
    args = argparse.Namespace(
        discovery_cache=None,
        ep1_noise_psk=None,
        ep1_host=[],
        no_ep1_zeroconf=True,
    )
    return TestClient(create_app(args))


def _snapshot(*, device_id: str = "98:25:4a:64:ac:90") -> KasaMotionTuningSnapshot:
    return KasaMotionTuningSnapshot(
        ambient_available=True,
        ambient_light=64,
        ambient_light_enabled=True,
        device_id=device_id,
        display_label=f"Garage light ({device_id})",
        display_name="Garage light",
        host="192.168.86.186",
        model="KS200M(US)",
        pir_enabled=True,
        pir_percent=-1.07,
        pir_range=KasaPirRange.MID,
        pir_range_choices=(
            KasaPirRange.FAR,
            KasaPirRange.MID,
            KasaPirRange.NEAR,
            KasaPirRange.CUSTOM,
        ),
        pir_threshold=50,
        pir_triggered=False,
    )
