"""Tests for the tailwind UI action endpoints + their helpers.

Covers:

* ``POST /v1/ui/tailwind/doors/{device_id}/open``
* ``POST /v1/ui/tailwind/doors/{device_id}/close``
* ``POST /v1/ui/tailwind/close-all``

and the underlying helpers (:func:`bulk_close_tailwind_apply`,
:func:`find_tailwind_by_identifier`, :func:`build_tailwind_device_view`)
plus the global-bulk endpoint after PR 5 expanded it to also close
tailwind doors.

Mock-based, no LAN traffic. The fake door records every ``open`` /
``close`` call and updates ``is_open`` / ``is_closed`` so a follow-up
:func:`build_tailwind_device_view` reflects the new position.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import kasa_discovery_store
from app.api.app import create_app
from app.api.ui_state import (
    build_tailwind_device_view,
    bulk_close_tailwind_apply,
    bulk_off_global_apply,
    find_tailwind_by_identifier,
)
from app.domesti_bot_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager


class _FakeDoor:
    """Mimics the slice of :class:`GotailwindDevice` the action helpers touch.

    Tracks position so a subsequent ``GET`` (and
    :func:`build_tailwind_device_view`) sees the new state without going
    through ``mgr.fetch()``.
    """

    def __init__(self, identifier: str, label: str, *, is_open: bool) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_open = is_open
        self.is_closed = not is_open
        self.calls: list[str] = []

    async def close(self) -> None:
        self.calls.append("close")
        self.is_open = False
        self.is_closed = True

    async def open(self) -> None:
        self.calls.append("open")
        self.is_open = True
        self.is_closed = False


class _FakeKasa:
    def __init__(self, host: str, label: str, *, is_on: bool) -> None:
        self._kDevice = MagicMock()
        self._kDevice.host = host
        self.preferred_label = label
        self.is_on = is_on
        self.calls: list[str] = []

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    return cast(KasaDeviceManager, mgr)


def _state(
    *,
    kasa_devices: list[_FakeKasa] | None = None,
    tailwind_doors: list[_FakeDoor] | None = None,
    cache_path: Path | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr(kasa_devices or []),
        sonos_mgr=None,
        tailwind_mgr=_tailwind_mgr(tailwind_doors) if tailwind_doors else None,
        androidtv_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def _tailwind_mgr(doors: list[_FakeDoor]) -> GotailwindDeviceManager:
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(doors)
    return cast(GotailwindDeviceManager, mgr)


def test_build_tailwind_device_view_raises_keyerror_for_unknown_door() -> None:
    state = _state(tailwind_doors=[_FakeDoor("door-1", "Left", is_open=False)])
    assert state.tailwind_mgr is not None
    with pytest.raises(KeyError):
        build_tailwind_device_view(
            state.tailwind_mgr, device_id="door-99", cache_path=None
        )


def test_build_tailwind_device_view_reflects_position_and_exclusion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="tailwind", canonical_key="door-1", exclude_from_global=True
    )
    door = _FakeDoor("door-1", "Left", is_open=True)
    state = _state(tailwind_doors=[door], cache_path=db)
    assert state.tailwind_mgr is not None
    view = build_tailwind_device_view(
        state.tailwind_mgr, device_id="door-1", cache_path=db
    )
    assert view.id == "door-1"
    assert view.family_id == "tailwind"
    assert view.kind == "door"
    assert view.state == "open"
    assert view.exclude_from_global is True


@pytest.mark.asyncio
async def test_bulk_close_tailwind_apply_closes_open_doors_only() -> None:
    open_door = _FakeDoor("door-1", "A", is_open=True)
    closed_door = _FakeDoor("door-2", "B", is_open=False)
    state = _state(tailwind_doors=[open_door, closed_door])
    affected, skipped = await bulk_close_tailwind_apply(state)
    assert affected == ["door-1"]
    assert skipped == []
    assert open_door.calls == ["close"]
    assert closed_door.calls == []
    assert open_door.is_closed is True
    assert closed_door.is_closed is True


@pytest.mark.asyncio
async def test_bulk_close_tailwind_apply_returns_empty_when_manager_absent() -> None:
    state = _state(kasa_devices=[])
    affected, skipped = await bulk_close_tailwind_apply(state)
    assert affected == []
    assert skipped == []


@pytest.mark.asyncio
async def test_bulk_close_tailwind_apply_ignores_exclude_from_global(
    tmp_path: Path,
) -> None:
    """Family-level "close all" must hit even excluded doors — the user
    clicked the family-wide button explicitly."""

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="tailwind", canonical_key="door-1", exclude_from_global=True
    )
    a = _FakeDoor("door-1", "Excluded", is_open=True)
    b = _FakeDoor("door-2", "Normal", is_open=True)
    state = _state(tailwind_doors=[a, b], cache_path=db)
    affected, _ = await bulk_close_tailwind_apply(state)
    assert affected == ["door-1", "door-2"]
    assert a.calls == ["close"]
    assert b.calls == ["close"]


@pytest.mark.asyncio
async def test_bulk_off_global_apply_mixes_kasa_and_tailwind(tmp_path: Path) -> None:
    """Global bulk-off targets both families and labels each entry by family."""

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="tailwind", canonical_key="door-2", exclude_from_global=True
    )
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.2", exclude_from_global=True
    )
    state = _state(
        kasa_devices=[
            _FakeKasa("10.0.0.1", "K1", is_on=True),
            _FakeKasa("10.0.0.2", "K2-excl", is_on=True),
        ],
        tailwind_doors=[
            _FakeDoor("door-1", "D1", is_open=True),
            _FakeDoor("door-2", "D2-excl", is_open=True),
        ],
        cache_path=db,
    )
    affected, skipped = await bulk_off_global_apply(state, cache_path=db)
    assert affected == [("kasa", "10.0.0.1"), ("tailwind", "door-1")]
    assert skipped == [("kasa", "10.0.0.2"), ("tailwind", "door-2")]


def test_find_tailwind_by_identifier_returns_match_or_none() -> None:
    door = _FakeDoor("door-1", "Left", is_open=True)
    state = _state(tailwind_doors=[door])
    assert state.tailwind_mgr is not None
    assert find_tailwind_by_identifier(state.tailwind_mgr, "door-1") is door  # type: ignore[comparison-overlap]
    assert find_tailwind_by_identifier(state.tailwind_mgr, "door-99") is None
    assert find_tailwind_by_identifier(state.tailwind_mgr, "") is None
    assert find_tailwind_by_identifier(state.tailwind_mgr, "  door-1  ") is door  # type: ignore[comparison-overlap]


def test_post_tailwind_close_all_closes_every_door() -> None:
    a = _FakeDoor("door-1", "A", is_open=True)
    b = _FakeDoor("door-2", "B", is_open=True)
    client, app = _client()
    app.state.device_state = _state(tailwind_doors=[a, b])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/close-all")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"affected": ["door-1", "door-2"], "skipped": []}
    assert a.is_closed is True
    assert b.is_closed is True


def test_post_tailwind_close_all_returns_empty_when_every_door_already_closed() -> None:
    closed = _FakeDoor("door-1", "A", is_open=False)
    client, app = _client()
    app.state.device_state = _state(tailwind_doors=[closed])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/close-all")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"affected": [], "skipped": []}
    assert closed.calls == []


def test_post_tailwind_close_all_returns_empty_when_manager_absent() -> None:
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/close-all")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"affected": [], "skipped": []}


def test_post_tailwind_close_door_returns_404_for_unknown_device() -> None:
    client, app = _client()
    app.state.device_state = _state(
        tailwind_doors=[_FakeDoor("door-1", "Left", is_open=True)]
    )
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/doors/door-99/close")
    assert r.status_code == HTTPStatus.NOT_FOUND
    assert "door-99" in r.json()["detail"]


def test_post_tailwind_close_door_returns_404_when_manager_absent() -> None:
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/doors/door-1/close")
    assert r.status_code == HTTPStatus.NOT_FOUND
    assert "Tailwind manager" in r.json()["detail"]


def test_post_tailwind_close_door_succeeds_and_returns_refreshed_view() -> None:
    door = _FakeDoor("door-1", "Left", is_open=True)
    client, app = _client()
    app.state.device_state = _state(tailwind_doors=[door])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/doors/door-1/close")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["device"]["id"] == "door-1"
    assert body["device"]["family_id"] == "tailwind"
    assert body["device"]["kind"] == "door"
    assert body["device"]["state"] == "closed"
    assert door.is_closed is True
    assert door.calls == ["close"]


def test_post_tailwind_open_door_returns_404_when_manager_absent() -> None:
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/doors/door-1/open")
    assert r.status_code == HTTPStatus.NOT_FOUND


def test_post_tailwind_open_door_succeeds_and_returns_refreshed_view() -> None:
    door = _FakeDoor("door-1", "Left", is_open=False)
    client, app = _client()
    app.state.device_state = _state(tailwind_doors=[door])
    app.state.discovery_error = None
    r = client.post("/v1/ui/tailwind/doors/door-1/open")
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["device"]["state"] == "open"
    assert door.is_open is True
    assert door.calls == ["open"]


def test_post_tailwind_actions_return_503_while_discovery_in_progress() -> None:
    """Same lifespan contract as the kasa endpoints."""

    client, _app = _client()
    paths: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", "/v1/ui/tailwind/close-all", {}),
        ("POST", "/v1/ui/tailwind/doors/door-1/close", {}),
        ("POST", "/v1/ui/tailwind/doors/door-1/open", {}),
    ]
    for method, path, body in paths:
        r = client.request(method, path, json=body)
        assert r.status_code == HTTPStatus.SERVICE_UNAVAILABLE, (
            f"{method} {path} → {r.status_code}"
        )
        assert r.headers.get("Retry-After") == "2"


def test_tailwind_endpoints_appear_in_openapi_schema() -> None:
    client, _app = _client()
    paths = client.get("/openapi.json").json().get("paths", {})
    assert "/v1/ui/tailwind/close-all" in paths
    assert "/v1/ui/tailwind/doors/{device_id}/close" in paths
    assert "/v1/ui/tailwind/doors/{device_id}/open" in paths
