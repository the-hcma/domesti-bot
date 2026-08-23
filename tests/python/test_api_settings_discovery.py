"""Tests for Settings discovery status + refresh routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.discovery_refresh import (
    DiscoveryDeviceSnapshot,
    DiscoveryFamilyResult,
    DiscoveryFamilyStatus,
    DiscoveryRefreshResult,
    DiscoverySettingsStatus,
)
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def _state(tmp_path: Path) -> DeviceManagersState:
    mgr = KasaDeviceManager(discovery_cache_path=tmp_path / "discovery.sqlite")
    mgr.rediscover = AsyncMock()  # type: ignore[method-assign]
    return DeviceManagersState(
        kasa_mgr=mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=tmp_path / "discovery.sqlite",
        args=argparse.Namespace(discovery_cache=str(tmp_path / "discovery.sqlite")),
    )


def test_get_discovery_settings_requires_ready_managers(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/discovery")
    assert r.status_code == HTTPStatus.SERVICE_UNAVAILABLE


def test_get_discovery_settings_returns_family_status(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    state = _state(tmp_path)
    status = DiscoverySettingsStatus(
        families=(
            DiscoveryFamilyStatus(
                available=True,
                device_count=1,
                devices=(
                    DiscoveryDeviceSnapshot(
                        device_id="aa:bb:cc:dd:ee:01",
                        display="Kitchen Plug (aa:bb:cc:dd:ee:01)",
                        preferred_label="Kitchen Plug",
                    ),
                ),
                family_id="kasa",
                label="Kasa",
                last_discovery_source="cache",
            ),
            DiscoveryFamilyStatus(
                available=False,
                device_count=0,
                devices=(),
                family_id="sonos",
                label="Sonos",
                last_discovery_source=None,
            ),
        )
    )
    with (
        runtime.temporary_device_state(state),
        patch(
            "app.api.settings_routes.discovery_settings_status",
            return_value=status,
        ),
    ):
        r = client.get("/v1/settings/discovery")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["families"][0]["family_id"] == "kasa"
    assert body["families"][0]["device_count"] == 1
    assert body["families"][0]["last_discovery_source"] == "cache"
    assert body["families"][0]["devices"][0]["display"] == "Kitchen Plug (aa:bb:cc:dd:ee:01)"
    assert len(body["families"][0]["devices"]) == body["families"][0]["device_count"]
    assert body["families"][1]["available"] is False


def test_post_discovery_refresh_returns_new_devices(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    state = _state(tmp_path)
    new_device = DiscoveryDeviceSnapshot(
        device_id="aa:bb:cc:dd:ee:02",
        display="Porch Plug (aa:bb:cc:dd:ee:02)",
        preferred_label="Porch Plug",
    )
    result = DiscoveryRefreshResult(
        families=(
            DiscoveryFamilyResult(
                device_count=2,
                devices=(new_device,),
                error=None,
                family_id="kasa",
                label="Kasa",
                new_devices=(new_device,),
                ok=True,
                skipped=False,
                skip_detail=None,
                source="discovery",
            ),
        ),
        new_devices=(new_device,),
    )
    with (
        runtime.temporary_device_state(state),
        patch(
            "app.api.settings_routes.refresh_all_device_discovery",
            new=AsyncMock(return_value=result),
        ) as refresh_mock,
    ):
        r = client.post("/v1/settings/discovery/refresh")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["new_devices"][0]["display"] == "Porch Plug (aa:bb:cc:dd:ee:02)"
    assert body["families"][0]["ok"] is True
    assert body["families"][0]["source"] == "discovery"
    assert body["families"][0]["devices"][0]["display"] == "Porch Plug (aa:bb:cc:dd:ee:02)"
    refresh_mock.assert_awaited_once()
