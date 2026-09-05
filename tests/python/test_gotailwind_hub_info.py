"""GoTailwind hub hardware identity: manager metadata + ``GET /v1/settings/tailwind/hub-info``."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from gotailwind.const import TailwindDoorState

from app.api.app import create_app
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager, _HubMetadata
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def _state(tailwind_mgr: GotailwindDeviceManager | None, *, db: Path) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=KasaDeviceManager(discovery_cache_path=db),
        sonos_mgr=None,
        tailwind_mgr=tailwind_mgr,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(discovery_cache=str(db)),
    )


@pytest.mark.asyncio
async def test_fetch_populates_hub_metadata_then_disconnect_clears_it() -> None:
    mgr = GotailwindDeviceManager(token="123456", host="192.168.1.40")

    door = MagicMock()
    door.door_id = "garage"
    door.index = 0
    door.state = TailwindDoorState.CLOSED
    status = MagicMock()
    status.doors = {"garage": door}
    status.device_id = "_94_b5_55_c_81_50_"
    status.firmware_version = "10.80"
    status.number_of_doors = 2
    status.product = "iQ3"
    status.protocol_version = "0.1"

    fake_tw = MagicMock()
    fake_tw.__aenter__ = AsyncMock(return_value=fake_tw)
    fake_tw.status = AsyncMock(return_value=status)
    fake_tw.close = AsyncMock()

    with (
        patch("app.gotailwind_device_manager.Tailwind", return_value=fake_tw),
        patch(
            "app.gotailwind_device_manager.lookup_mac_via_arp",
            return_value="94:b5:55:0c:81:50",
        ),
    ):
        await mgr.fetch()

    assert mgr.product == "iQ3"
    assert mgr.firmware_version == "10.80"
    assert mgr.protocol_version == "0.1"
    assert mgr.device_id == "_94_b5_55_c_81_50_"
    assert mgr.number_of_doors == 2

    await mgr.disconnect()

    assert mgr.product is None
    assert mgr.firmware_version is None
    assert mgr.device_id is None
    assert mgr.number_of_doors is None


@pytest.mark.asyncio
async def test_fetch_leaves_hub_metadata_unset_when_door_finalize_raises() -> None:
    """A raise after ``status()`` but before the roster is finalized must not set ``_hub_metadata``."""
    mgr = GotailwindDeviceManager(token="123456", host="192.168.1.40")

    door = MagicMock()
    door.door_id = "garage"
    door.index = 0
    door.state = TailwindDoorState.CLOSED
    status = MagicMock()
    status.doors = {"garage": door}
    status.product = "iQ3"

    fake_tw = MagicMock()
    fake_tw.__aenter__ = AsyncMock(return_value=fake_tw)
    fake_tw.status = AsyncMock(return_value=status)
    fake_tw.close = AsyncMock()

    with (
        patch("app.gotailwind_device_manager.Tailwind", return_value=fake_tw),
        patch(
            "app.gotailwind_device_manager.lookup_mac_via_arp",
            return_value="94:b5:55:0c:81:50",
        ),
        patch.object(
            GotailwindDeviceManager,
            "_finalize_tailwind_devices",
            side_effect=RuntimeError("sqlite is down"),
        ),
        pytest.raises(RuntimeError),
    ):
        await mgr.fetch()

    assert mgr.product is None
    assert mgr.device_id is None


def test_hub_info_route_reports_not_reachable_without_manager(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/tailwind/hub-info")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["reachable"] is False
    assert body["product"] is None
    assert body["firmware_version"] is None


def test_hub_info_route_reports_not_reachable_for_half_fetched_manager(tmp_path: Path) -> None:
    """A manager left with ``host`` set but no hub metadata (failed in-place rediscover) is not reachable."""
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)

    mgr = GotailwindDeviceManager(token="123456", host="192.168.1.40")
    mgr._host = "192.168.1.40"
    mgr._hub_mac = "94:b5:55:0c:81:50"
    mgr._hub_metadata = None

    with runtime.temporary_device_state(_state(mgr, db=db)):
        r = client.get("/v1/settings/tailwind/hub-info")

    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["reachable"] is False
    assert body["product"] is None
    assert body["host"] is None


def test_hub_info_route_reports_metadata_from_live_manager(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)

    mgr = GotailwindDeviceManager(token="123456", host="192.168.1.40")
    mgr._host = "192.168.1.40"
    mgr._hub_mac = "94:b5:55:0c:81:50"
    mgr._hub_metadata = _HubMetadata(
        device_id="_94_b5_55_c_81_50_",
        firmware_version="10.80",
        number_of_doors=2,
        product="iQ3",
        protocol_version="0.1",
    )

    with runtime.temporary_device_state(_state(mgr, db=db)):
        r = client.get("/v1/settings/tailwind/hub-info")

    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["reachable"] is True
    assert body["product"] == "iQ3"
    assert body["firmware_version"] == "10.80"
    assert body["protocol_version"] == "0.1"
    assert body["device_id"] == "_94_b5_55_c_81_50_"
    assert body["hub_mac"] == "94:b5:55:0c:81:50"
    assert body["host"] == "192.168.1.40"
    assert body["number_of_doors"] == 2
