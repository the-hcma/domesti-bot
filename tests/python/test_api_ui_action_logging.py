"""Tests for ``[ui-action]`` logging on web UI device action routes."""

from __future__ import annotations

import argparse
import logging
from http import HTTPStatus
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.preferred_label = label
        self.is_on = is_on

    async def turn_off(self) -> None:
        self.is_on = False

    async def turn_on(self) -> None:
        self.is_on = True


def _client() -> TestClient:
    app = create_app(argparse.Namespace())
    return TestClient(app)


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    return cast(KasaDeviceManager, mgr)


def _state(kasa_devices: list[_FakeKasa]) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr(kasa_devices),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )


def test_kasa_toggle_emits_ui_action_log(caplog: pytest.LogCaptureFixture) -> None:
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=True)
    client = _client()
    runtime.device_state = _state([fake])
    runtime.discovery_error = None

    with caplog.at_level(logging.INFO, logger="app.api.ui_action_logging"):
        response = client.post(
            "/v1/ui/kasa/devices/10.0.0.1/toggle",
            json={"on": False},
        )

    assert response.status_code == HTTPStatus.OK
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message.startswith("[ui-action] toggle ")
    assert "client=testclient" in message
    assert "family=kasa" in message
    assert "device=10.0.0.1" in message
    assert "on=False" in message


def test_global_bulk_off_emits_ui_action_log(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    from app import device_discovery_store

    db = tmp_path / "ui.sqlite"
    device_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.2", exclude_from_global=True
    )
    a = _FakeKasa("10.0.0.1", "Keep", is_on=True)
    b = _FakeKasa("10.0.0.2", "Excluded", is_on=True)
    client = _client()
    runtime.device_state = DeviceManagersState(
        kasa_mgr=_kasa_mgr([a, b]),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    runtime.discovery_error = None

    with caplog.at_level(logging.INFO, logger="app.api.ui_action_logging"):
        response = client.post("/v1/ui/global/bulk-off")

    assert response.status_code == HTTPStatus.OK
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message.startswith("[ui-action] bulk_off ")
    assert "family=global" in message
    assert "affected=1" in message
    assert "skipped=1" in message


def test_kasa_toggle_404_does_not_emit_ui_action_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _client()
    runtime.device_state = _state([_FakeKasa("10.0.0.1", "Lamp", is_on=True)])
    runtime.discovery_error = None

    with caplog.at_level(logging.INFO, logger="app.api.ui_action_logging"):
        response = client.post(
            "/v1/ui/kasa/devices/10.0.0.99/toggle",
            json={"on": False},
        )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert caplog.records == []
