"""Tests for the UI action endpoints + their helpers in :mod:`app.api.ui_state`.

Covers:

* ``POST /v1/ui/kasa/devices/{device_id}/toggle``
* ``POST /v1/ui/kasa/bulk-off``
* ``POST /v1/ui/global/bulk-off``
* ``PUT  /v1/ui/preferences/{family_id}/{device_id}``

and the underlying helpers (:func:`bulk_off_kasa_apply`,
:func:`bulk_off_global_apply`, :func:`find_kasa_by_host`,
:func:`build_kasa_device_view`).

All mock-based, no LAN traffic. The fake kasa device records every
``turn_on`` / ``turn_off`` call so we can assert the helper actually fired
side effects (per ``AGENTS.md``: "Each test asserts an observable outcome
— not merely that a mock was called").
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import kasa_discovery_store
from app.api.app import create_app
from app.api.ui_state import (
    build_kasa_device_view,
    bulk_off_global_apply,
    bulk_off_kasa_apply,
    find_kasa_by_host,
)
from app.device_manager_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager


class _FakeKasa:
    """Mimics the slice of :class:`KasaDevice` that the action helpers touch.

    Records ``turn_on`` / ``turn_off`` invocations so tests can assert on
    side effects directly, not just that a method was called. Keeps the
    cached ``is_on`` consistent with the most recent action so a follow-up
    :func:`build_kasa_device_view` call sees the new state.
    """

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
    kasa_devices: list[_FakeKasa],
    cache_path: Path | None = None,
    tailwind_mgr: GotailwindDeviceManager | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr(kasa_devices),
        sonos_mgr=None,
        tailwind_mgr=tailwind_mgr,
        androidtv_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def _tailwind_mgr_with(door_id: str) -> GotailwindDeviceManager:
    door = MagicMock()
    door.identifier = door_id
    door.preferred_label = door_id
    door.is_open = True
    door.is_closed = False
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = (door,)
    return cast(GotailwindDeviceManager, mgr)


def test_build_kasa_device_view_raises_keyerror_for_unknown_host() -> None:
    state = _state(kasa_devices=[_FakeKasa("10.0.0.1", "Lamp", is_on=True)])
    with pytest.raises(KeyError):
        build_kasa_device_view(state.kasa_mgr, host="10.0.0.99", cache_path=None)


def test_build_kasa_device_view_reflects_current_is_on_and_exclusion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.1", exclude_from_global=True
    )
    fake = _FakeKasa("10.0.0.1", "Lamp", is_on=False)
    state = _state(kasa_devices=[fake], cache_path=db)
    view = build_kasa_device_view(state.kasa_mgr, host="10.0.0.1", cache_path=db)
    assert view.id == "10.0.0.1"
    assert view.label == "Lamp"
    assert view.state == "off"
    assert view.exclude_from_global is True


@pytest.mark.asyncio
async def test_bulk_off_global_apply_skips_excluded_devices(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.2", exclude_from_global=True
    )
    a = _FakeKasa("10.0.0.1", "Keep", is_on=True)
    b = _FakeKasa("10.0.0.2", "Excluded", is_on=True)
    c = _FakeKasa("10.0.0.3", "Also keep", is_on=True)
    state = _state(kasa_devices=[a, b, c], cache_path=db)
    affected, skipped = await bulk_off_global_apply(state, cache_path=db)
    # PR5 gave global bulk-off a richer return shape so it can mix kasa
    # hosts and tailwind door ids without ambiguity.
    assert affected == [("kasa", "10.0.0.1"), ("kasa", "10.0.0.3")]
    assert skipped == [("kasa", "10.0.0.2")]
    assert a.is_on is False
    assert b.is_on is True
    assert c.is_on is False
    assert a.calls == ["off"]
    assert b.calls == []
    assert c.calls == ["off"]


@pytest.mark.asyncio
async def test_bulk_off_global_apply_with_no_cache_path_treats_no_one_as_excluded() -> None:
    a = _FakeKasa("10.0.0.1", "A", is_on=True)
    b = _FakeKasa("10.0.0.2", "B", is_on=True)
    state = _state(kasa_devices=[a, b])
    affected, skipped = await bulk_off_global_apply(state, cache_path=None)
    assert affected == [("kasa", "10.0.0.1"), ("kasa", "10.0.0.2")]
    assert skipped == []


@pytest.mark.asyncio
async def test_bulk_off_kasa_apply_ignores_exclude_from_global(tmp_path: Path) -> None:
    """Family-level "all kasa off" must hit even devices that are
    excluded from the *global* action — the user explicitly asked for the
    family-wide button."""

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.1", exclude_from_global=True
    )
    a = _FakeKasa("10.0.0.1", "Excluded", is_on=True)
    b = _FakeKasa("10.0.0.2", "Normal", is_on=True)
    state = _state(kasa_devices=[a, b], cache_path=db)
    affected, skipped = await bulk_off_kasa_apply(state)
    assert affected == ["10.0.0.1", "10.0.0.2"]
    assert skipped == []
    assert a.is_on is False
    assert b.is_on is False


@pytest.mark.asyncio
async def test_bulk_off_kasa_apply_returns_sorted_affected_list() -> None:
    devices = [
        _FakeKasa("10.0.0.3", "C", is_on=True),
        _FakeKasa("10.0.0.1", "A", is_on=True),
        _FakeKasa("10.0.0.2", "B", is_on=True),
    ]
    state = _state(kasa_devices=devices)
    affected, _ = await bulk_off_kasa_apply(state)
    assert affected == ["10.0.0.1", "10.0.0.2", "10.0.0.3"]


def test_find_kasa_by_host_returns_match_or_none() -> None:
    fake = _FakeKasa("192.168.1.50", "Lamp", is_on=True)
    state = _state(kasa_devices=[fake])
    assert find_kasa_by_host(state.kasa_mgr, "192.168.1.50") is fake  # type: ignore[comparison-overlap]
    assert find_kasa_by_host(state.kasa_mgr, "  192.168.1.50  ") is fake  # type: ignore[comparison-overlap]
    assert find_kasa_by_host(state.kasa_mgr, "192.168.1.99") is None
    assert find_kasa_by_host(state.kasa_mgr, "") is None
    assert find_kasa_by_host(state.kasa_mgr, "   ") is None


def test_post_global_bulk_off_returns_affected_and_skipped(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.2", exclude_from_global=True
    )
    a = _FakeKasa("10.0.0.1", "Keep", is_on=True)
    b = _FakeKasa("10.0.0.2", "Excluded", is_on=True)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[a, b], cache_path=db)
    app.state.discovery_error = None

    response = client.post("/v1/ui/global/bulk-off")
    assert response.status_code == 200
    body = response.json()
    # PR5 evolved the response: each entry is a ``{family_id, device_id}``
    # object so kasa/tailwind can coexist in the same payload.
    assert body == {
        "affected": [{"family_id": "kasa", "device_id": "10.0.0.1"}],
        "skipped": [{"family_id": "kasa", "device_id": "10.0.0.2"}],
    }
    assert a.is_on is False
    assert b.is_on is True


def test_post_kasa_bulk_off_turns_off_every_kasa_device() -> None:
    a = _FakeKasa("10.0.0.1", "A", is_on=True)
    b = _FakeKasa("10.0.0.2", "B", is_on=True)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[a, b])
    app.state.discovery_error = None

    response = client.post("/v1/ui/kasa/bulk-off")
    assert response.status_code == 200
    body = response.json()
    assert body == {"affected": ["10.0.0.1", "10.0.0.2"], "skipped": []}
    assert a.is_on is False
    assert b.is_on is False


def test_post_kasa_toggle_returns_404_for_unknown_device() -> None:
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[_FakeKasa("10.0.0.1", "Lamp", is_on=True)]
    )
    app.state.discovery_error = None
    r = client.post(
        "/v1/ui/kasa/devices/10.0.0.99/toggle",
        json={"on": False},
    )
    assert r.status_code == 404
    detail = r.json()["detail"]
    assert "10.0.0.99" in detail


def test_post_kasa_toggle_turns_device_off_and_returns_refreshed_view(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.1", exclude_from_global=False
    )
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=True)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[fake], cache_path=db)
    app.state.discovery_error = None

    r = client.post(
        "/v1/ui/kasa/devices/10.0.0.1/toggle",
        json={"on": False},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["device"]["id"] == "10.0.0.1"
    assert body["device"]["state"] == "off"
    assert body["device"]["family_id"] == "kasa"
    assert body["device"]["exclude_from_global"] is False
    assert fake.is_on is False
    assert fake.calls == ["off"]


def test_post_kasa_toggle_turns_device_on() -> None:
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=False)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[fake])
    app.state.discovery_error = None
    r = client.post("/v1/ui/kasa/devices/10.0.0.1/toggle", json={"on": True})
    assert r.status_code == 200
    assert r.json()["device"]["state"] == "on"
    assert fake.is_on is True


def test_post_kasa_toggle_with_invalid_body_returns_422() -> None:
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=False)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[fake])
    app.state.discovery_error = None
    r = client.post("/v1/ui/kasa/devices/10.0.0.1/toggle", json={"power": True})
    assert r.status_code == 422


def test_put_ui_preference_persists_kasa_exclusion(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    fake = _FakeKasa("10.0.0.1", "Desk", is_on=True)
    client, app = _client()
    app.state.device_state = _state(kasa_devices=[fake], cache_path=db)
    app.state.discovery_error = None

    r = client.put(
        "/v1/ui/preferences/kasa/10.0.0.1",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 200
    assert r.json() == {
        "family_id": "kasa",
        "device_id": "10.0.0.1",
        "exclude_from_global": True,
    }
    assert kasa_discovery_store.load_ui_preferences(db) == [
        ("kasa", "10.0.0.1", True),
    ]

    r = client.put(
        "/v1/ui/preferences/kasa/10.0.0.1",
        json={"exclude_from_global": False},
    )
    assert r.status_code == 200
    assert kasa_discovery_store.load_ui_preferences(db) == [
        ("kasa", "10.0.0.1", False),
    ]


def test_put_ui_preference_returns_404_for_unknown_kasa_device(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[_FakeKasa("10.0.0.1", "Lamp", is_on=True)],
        cache_path=db,
    )
    app.state.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/kasa/10.0.0.99",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 404


def test_put_ui_preference_returns_404_for_unknown_tailwind_device(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[],
        cache_path=db,
        tailwind_mgr=_tailwind_mgr_with("door-1"),
    )
    app.state.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/tailwind/door-99",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 404


def test_put_ui_preference_returns_400_for_unknown_family(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[_FakeKasa("10.0.0.1", "Lamp", is_on=True)],
        cache_path=db,
    )
    app.state.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/zigbee/whatever",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 400
    assert "zigbee" in r.json()["detail"]


def test_put_ui_preference_returns_409_when_no_discovery_cache_configured() -> None:
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[_FakeKasa("10.0.0.1", "Lamp", is_on=True)],
        cache_path=None,
    )
    app.state.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/kasa/10.0.0.1",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 409
    assert "discovery cache" in r.json()["detail"]


def test_put_ui_preference_persists_tailwind_exclusion(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, app = _client()
    app.state.device_state = _state(
        kasa_devices=[],
        cache_path=db,
        tailwind_mgr=_tailwind_mgr_with("door-1"),
    )
    app.state.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/tailwind/door-1",
        json={"exclude_from_global": True},
    )
    assert r.status_code == 200
    assert kasa_discovery_store.load_ui_preferences(db) == [
        ("tailwind", "door-1", True),
    ]


def test_action_endpoints_return_503_while_discovery_in_progress() -> None:
    """All four new endpoints share the ``DeviceState`` dependency, so
    they must all 503 with ``Retry-After: 2`` while discovery hasn't
    finished. Verifies the dependency is correctly wired (a missing
    ``Depends(_device_state)`` would 200 with a misleading payload)."""

    client, _app = _client()
    bodies: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", "/v1/ui/global/bulk-off", {}),
        ("POST", "/v1/ui/kasa/bulk-off", {}),
        ("POST", "/v1/ui/kasa/devices/10.0.0.1/toggle", {"on": True}),
        ("PUT", "/v1/ui/preferences/kasa/10.0.0.1", {"exclude_from_global": True}),
    ]
    for method, path, body in bodies:
        r = client.request(method, path, json=body)
        assert r.status_code == 503, f"{method} {path} → {r.status_code}"
        assert r.headers.get("Retry-After") == "2"


def test_new_endpoints_appear_in_openapi_schema() -> None:
    client, _app = _client()
    paths = client.get("/openapi.json").json().get("paths", {})
    assert "/v1/ui/global/bulk-off" in paths
    assert "/v1/ui/kasa/bulk-off" in paths
    assert "/v1/ui/kasa/devices/{device_id}/toggle" in paths
    assert "/v1/ui/preferences/{family_id}/{device_id}" in paths
