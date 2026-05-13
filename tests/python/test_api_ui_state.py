"""Tests for :func:`app.api.ui_state.build_ui_state` and ``GET /v1/ui/state``.

The endpoint is the read-only join of three sources:

* ``state.kasa_mgr.switches``  → kasa tiles
* ``state.tailwind_mgr.doors`` → tailwind tiles (when the manager is present)
* ``ui_preferences`` SQLite rows at ``state.cache_path`` → ``exclude_from_global``

These tests exercise the helper directly with fake managers (no network) and
the HTTP route through ``TestClient`` with ``app.state.device_state``
populated manually so we never trigger the discovery lifespan (mirrors the
pattern in ``test_api_landing.py``).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import kasa_discovery_store
from app.api.app import create_app
from app.api.schemas import UIDeviceOut, UIFamilyOut, UIStateOut
from app.api.ui_state import build_ui_state
from app.device_manager_cli import DeviceManagersState
from app.gotailwind_device_manager import GotailwindDeviceManager
from app.kasa_device_manager import KasaDeviceManager


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def _fake_kasa_mgr(devices: list[tuple[str, str, bool]]) -> KasaDeviceManager:
    """Return a Mock manager whose ``.switches`` is the supplied tuples.

    Each input tuple is ``(host, preferred_label, is_on)`` — exactly the
    fields :func:`build_ui_state._kasa_devices` reads.
    """

    fakes: list[Any] = []
    for host, label, is_on in devices:
        kd = MagicMock()
        kd._kDevice.host = host
        kd.preferred_label = label
        kd.is_on = is_on
        fakes.append(kd)
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(fakes)
    return cast(KasaDeviceManager, mgr)


def _fake_tailwind_mgr(
    doors: list[tuple[str, str, bool, bool]],
) -> GotailwindDeviceManager:
    """Return a Mock manager whose ``.doors`` is the supplied tuples.

    Each input tuple is ``(identifier, preferred_label, is_open, is_closed)``.
    Setting both ``is_open`` and ``is_closed`` to ``False`` simulates a
    transient state (``OPENING`` / ``CLOSING``); :func:`build_ui_state`
    must surface that as ``state="unknown"``.
    """

    fakes: list[Any] = []
    for ident, label, is_open, is_closed in doors:
        gd = MagicMock()
        gd.identifier = ident
        gd.preferred_label = label
        gd.is_open = is_open
        gd.is_closed = is_closed
        fakes.append(gd)
    mgr = MagicMock(spec=GotailwindDeviceManager)
    mgr.doors = tuple(fakes)
    return cast(GotailwindDeviceManager, mgr)


def _state(
    *,
    kasa_mgr: KasaDeviceManager,
    tailwind_mgr: GotailwindDeviceManager | None = None,
    cache_path: Path | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=kasa_mgr,
        sonos_mgr=None,
        tailwind_mgr=tailwind_mgr,
        androidtv_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def test_build_ui_state_emits_no_families_when_kasa_is_empty_and_no_tailwind() -> None:
    state = _state(kasa_mgr=_fake_kasa_mgr([]))
    out = build_ui_state(state, cache_path=None)
    assert out == UIStateOut(families=[])


def test_build_ui_state_emits_only_kasa_family_when_tailwind_manager_absent() -> None:
    state = _state(kasa_mgr=_fake_kasa_mgr([("192.168.1.10", "Desk", True)]))
    out = build_ui_state(state, cache_path=None)
    assert [f.id for f in out.families] == ["kasa"]
    assert out.families[0].label == "Lights & plugs"
    assert out.families[0].color == "#3B82F6"
    assert out.families[0].devices == [
        UIDeviceOut(
            id="192.168.1.10",
            family_id="kasa",
            label="Desk",
            kind="switch",
            state="on",
            exclude_from_global=False,
        )
    ]


def test_build_ui_state_emits_both_families_in_kasa_then_tailwind_order() -> None:
    state = _state(
        kasa_mgr=_fake_kasa_mgr([("192.168.1.10", "Desk", False)]),
        tailwind_mgr=_fake_tailwind_mgr([("door-1", "Left", False, True)]),
    )
    out = build_ui_state(state, cache_path=None)
    assert [f.id for f in out.families] == ["kasa", "tailwind"]
    assert out.families[1].label == "Garage doors"
    assert out.families[1].color == "#10B981"
    assert out.families[1].devices[0].state == "closed"


def test_build_ui_state_kasa_switch_state_maps_is_on_true_to_on_and_false_to_off() -> None:
    state = _state(
        kasa_mgr=_fake_kasa_mgr(
            [
                ("10.0.0.1", "Lamp", True),
                ("10.0.0.2", "Plug", False),
            ]
        ),
    )
    out = build_ui_state(state, cache_path=None)
    by_id = {d.id: d for d in out.families[0].devices}
    assert by_id["10.0.0.1"].state == "on"
    assert by_id["10.0.0.2"].state == "off"


def test_build_ui_state_kasa_skips_devices_with_blank_host() -> None:
    """A KasaDevice with no usable host can't be addressed; drop it instead of
    emitting a tile that points nowhere."""

    state = _state(
        kasa_mgr=_fake_kasa_mgr(
            [
                ("", "Ghost", True),
                ("   ", "Whitespace", True),
                ("192.168.1.42", "Real", True),
            ]
        ),
    )
    out = build_ui_state(state, cache_path=None)
    assert [d.id for d in out.families[0].devices] == ["192.168.1.42"]


def test_build_ui_state_tailwind_door_state_maps_open_closed_and_unknown() -> None:
    state = _state(
        kasa_mgr=_fake_kasa_mgr([]),
        tailwind_mgr=_fake_tailwind_mgr(
            [
                ("door-open", "Open", True, False),
                ("door-closed", "Closed", False, True),
                ("door-mid", "Moving", False, False),
            ]
        ),
    )
    out = build_ui_state(state, cache_path=None)
    by_id = {d.id: d for d in out.families[0].devices}
    assert by_id["door-open"].state == "open"
    assert by_id["door-closed"].state == "closed"
    assert by_id["door-mid"].state == "unknown"


def test_build_ui_state_devices_within_a_family_are_sorted_by_label_then_id() -> None:
    state = _state(
        kasa_mgr=_fake_kasa_mgr(
            [
                ("10.0.0.3", "Zebra", True),
                ("10.0.0.1", "alpha", True),
                ("10.0.0.2", "Bravo", True),
            ]
        ),
    )
    out = build_ui_state(state, cache_path=None)
    assert [d.label for d in out.families[0].devices] == ["alpha", "Bravo", "Zebra"]


def test_build_ui_state_cache_path_none_means_no_exclusions(tmp_path: Path) -> None:
    """Even if a SQLite store exists elsewhere, ``cache_path=None`` (e.g.
    ``--no-discovery-cache``) must skip the read entirely."""

    # Pre-seed a different file (this is *not* what we pass below):
    other_db = tmp_path / "other.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        other_db, backend="kasa", canonical_key="10.0.0.1", exclude_from_global=True
    )
    state = _state(kasa_mgr=_fake_kasa_mgr([("10.0.0.1", "Lamp", True)]))
    out = build_ui_state(state, cache_path=None)
    assert out.families[0].devices[0].exclude_from_global is False


def test_build_ui_state_excluded_keys_set_exclude_from_global_true(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="10.0.0.2", exclude_from_global=True
    )
    state = _state(
        kasa_mgr=_fake_kasa_mgr(
            [
                ("10.0.0.1", "Keep", True),
                ("10.0.0.2", "Exclude", True),
            ]
        ),
        cache_path=db,
    )
    out = build_ui_state(state, cache_path=db)
    by_id = {d.id: d for d in out.families[0].devices}
    assert by_id["10.0.0.1"].exclude_from_global is False
    assert by_id["10.0.0.2"].exclude_from_global is True


def test_build_ui_state_exclusions_dont_cross_families(tmp_path: Path) -> None:
    """A ``tailwind`` exclusion must not flip a same-keyed kasa device, and
    vice versa. ``backend`` is part of the composite primary key for a
    reason."""

    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="tailwind", canonical_key="left", exclude_from_global=True
    )
    state = _state(
        kasa_mgr=_fake_kasa_mgr([("left", "Left lamp", True)]),
        tailwind_mgr=_fake_tailwind_mgr([("left", "Left door", True, False)]),
        cache_path=db,
    )
    out = build_ui_state(state, cache_path=db)
    families = {f.id: f for f in out.families}
    assert families["kasa"].devices[0].exclude_from_global is False
    assert families["tailwind"].devices[0].exclude_from_global is True


def test_get_v1_ui_state_returns_payload_when_state_is_set(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="kasa", canonical_key="192.168.1.50", exclude_from_global=True
    )
    client, app = _client()
    state = _state(
        kasa_mgr=_fake_kasa_mgr(
            [
                ("192.168.1.50", "Living room", True),
                ("192.168.1.51", "Kitchen", False),
            ]
        ),
        cache_path=db,
    )
    app.state.device_state = state
    app.state.discovery_error = None

    response = client.get("/v1/ui/state")
    assert response.status_code == 200
    payload = response.json()
    assert "families" in payload
    assert len(payload["families"]) == 1
    fam = payload["families"][0]
    assert fam["id"] == "kasa"
    assert fam["color"] == "#3B82F6"
    assert {d["id"] for d in fam["devices"]} == {"192.168.1.50", "192.168.1.51"}
    by_id = {d["id"]: d for d in fam["devices"]}
    assert by_id["192.168.1.50"]["exclude_from_global"] is True
    assert by_id["192.168.1.50"]["state"] == "on"
    assert by_id["192.168.1.51"]["exclude_from_global"] is False
    assert by_id["192.168.1.51"]["state"] == "off"


def test_get_v1_ui_state_rejects_request_without_api_key_when_env_set(
    tmp_path: Path,
) -> None:
    """When ``DOMESTI_API_KEY`` is set, the endpoint must demand the header."""

    client, app = _client()
    state = _state(
        kasa_mgr=_fake_kasa_mgr([("10.0.0.1", "Lamp", True)]),
        cache_path=tmp_path / "ui.sqlite",
    )
    app.state.device_state = state
    app.state.discovery_error = None
    with patch.dict(os.environ, {"DOMESTI_API_KEY": "shh"}, clear=False):
        bad = client.get("/v1/ui/state")
        assert bad.status_code == 401
        ok = client.get("/v1/ui/state", headers={"X-Domesti-Api-Key": "shh"})
        assert ok.status_code == 200
        assert ok.json()["families"][0]["devices"][0]["id"] == "10.0.0.1"


def test_get_v1_ui_state_returns_503_with_retry_after_while_discovery_in_progress() -> None:
    client, _app = _client()
    response = client.get("/v1/ui/state")
    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "2"


def test_get_v1_ui_state_appears_in_openapi_schema() -> None:
    client, _app = _client()
    paths = client.get("/openapi.json").json().get("paths", {})
    assert "/v1/ui/state" in paths


def test_ui_state_out_is_a_pydantic_model_with_expected_fields() -> None:
    """Guard against accidental schema rename — the front-end consumes these
    field names verbatim."""

    fields = UIStateOut.model_fields
    assert set(fields.keys()) == {"families"}
    family_fields = UIFamilyOut.model_fields
    assert set(family_fields.keys()) == {"id", "label", "color", "devices"}
    device_fields = UIDeviceOut.model_fields
    assert set(device_fields.keys()) == {
        "id",
        "family_id",
        "label",
        "kind",
        "state",
        "exclude_from_global",
    }
