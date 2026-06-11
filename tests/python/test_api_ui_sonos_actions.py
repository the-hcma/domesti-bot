"""Tests for the Sonos UI action endpoints + their helpers.

Covers:

* ``POST /v1/ui/sonos/zones/{device_id}/toggle``
* ``POST /v1/ui/sonos/pause-all``

and the underlying helpers (:func:`bulk_pause_sonos_apply`,
:func:`find_sonos_by_identifier`, :func:`build_sonos_device_view`)
plus the global-bulk endpoint after we extended it to also pause
Sonos zones.

Mock-based, no LAN traffic. The fake zone records every ``pause`` /
``resume`` call and updates ``is_playing`` so a follow-up
:func:`build_sonos_device_view` reflects the new playback flag.
"""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import kasa_discovery_store
from app.api.app import create_app
from app.api.ui_state import (
    build_sonos_device_view,
    bulk_off_global_apply,
    bulk_pause_sonos_apply,
    find_sonos_by_identifier,
)
from app.domesti_bot_cli import DeviceManagersState
from app.server_runtime import runtime
from app.kasa_device_manager import KasaDeviceManager
from app.sonos_device_manager import (
    SonosDeviceManager,
    SonosTransitionUnavailableError,
)


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


class _FakeSonosZone:
    """Mimics the slice of :class:`SonosSpeakerDevice` the helpers touch.

    Tracks ``is_playing`` so a subsequent
    :func:`build_sonos_device_view` (and the post-action poll the
    real watcher would do) sees the new state without round-tripping
    through SoCo. Setting ``raise_transition_unavailable_on`` to
    ``"pause"`` / ``"resume"`` simulates the UPnP 701 case that the
    real device wraps as :class:`SonosTransitionUnavailableError` —
    the cache is updated to ``False`` first (mirrors the real device,
    which refreshes from a live UPnP read before raising) so the UI
    view shows the truth after the action.
    """

    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        is_playing: bool | None,
        raise_transition_unavailable_on: str | None = None,
    ) -> None:
        self.identifier = identifier
        self.preferred_label = label
        self.is_playing = is_playing
        self.stream_favorites: tuple = ()
        self.calls: list[str] = []
        self._raise_on = raise_transition_unavailable_on

    async def pause(self) -> None:
        self.calls.append("pause")
        if self._raise_on == "pause":
            self.is_playing = False
            raise SonosTransitionUnavailableError(
                f"{self.preferred_label!r} cannot pause"
            )
        self.is_playing = False

    async def resume(self, *, favorite_index: int = 0) -> None:
        self.calls.append(f"resume:{favorite_index}")
        if self._raise_on == "resume":
            self.is_playing = False
            raise SonosTransitionUnavailableError(
                f"{self.preferred_label!r} cannot resume"
            )
        self.is_playing = True


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def _kasa_mgr(devices: list[_FakeKasa]) -> KasaDeviceManager:
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(devices)
    return cast(KasaDeviceManager, mgr)


def _sonos_mgr(zones: list[_FakeSonosZone]) -> SonosDeviceManager:
    mgr = MagicMock(spec=SonosDeviceManager)
    mgr.players = tuple(zones)
    return cast(SonosDeviceManager, mgr)


def _state(
    *,
    sonos_zones: list[_FakeSonosZone] | None = None,
    kasa_devices: list[_FakeKasa] | None = None,
    cache_path: Path | None = None,
) -> DeviceManagersState:
    return DeviceManagersState(
        kasa_mgr=_kasa_mgr(kasa_devices or []),
        sonos_mgr=_sonos_mgr(sonos_zones) if sonos_zones is not None else None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        cache_path=cache_path,
        args=argparse.Namespace(),
    )


def test_build_sonos_device_view_raises_keyerror_for_unknown_zone() -> None:
    state = _state(
        sonos_zones=[_FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=True)]
    )
    assert state.sonos_mgr is not None
    with pytest.raises(KeyError):
        build_sonos_device_view(
            state.sonos_mgr, device_id="RINCON_ZZZZ", cache_path=None
        )


def test_build_sonos_device_view_reflects_is_playing_and_exclusion(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="sonos", canonical_key="RINCON_AAAA", exclude_from_global=True
    )
    zone = _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=False)
    state = _state(sonos_zones=[zone], cache_path=db)
    assert state.sonos_mgr is not None
    view = build_sonos_device_view(
        state.sonos_mgr, device_id="RINCON_AAAA", cache_path=db
    )
    assert view.id == "RINCON_AAAA"
    assert view.kind == "speaker"
    assert view.state == "paused"
    assert view.exclude_from_global is True


def test_build_sonos_device_view_unknown_when_is_playing_is_none() -> None:
    zone = _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=None)
    state = _state(sonos_zones=[zone])
    assert state.sonos_mgr is not None
    view = build_sonos_device_view(
        state.sonos_mgr, device_id="RINCON_AAAA", cache_path=None
    )
    assert view.state == "unknown"


@pytest.mark.asyncio
async def test_bulk_pause_sonos_apply_only_pauses_playing_zones() -> None:
    """Already-paused zones are skipped; unknown zones still get a pause attempt.

    Asserts the helper iterates :attr:`SonosDeviceManager.players` and
    skips only zones whose cached :attr:`is_playing` is ``False``.
    """

    playing = _FakeSonosZone("RINCON_A", "A", is_playing=True)
    paused = _FakeSonosZone("RINCON_B", "B", is_playing=False)
    unknown = _FakeSonosZone("RINCON_C", "C", is_playing=None)
    state = _state(sonos_zones=[playing, paused, unknown])
    affected, skipped = await bulk_pause_sonos_apply(state)
    assert affected == ["RINCON_A", "RINCON_C"]
    assert skipped == []
    assert playing.calls == ["pause"]
    assert paused.calls == []
    assert unknown.calls == ["pause"]


@pytest.mark.asyncio
async def test_bulk_pause_sonos_apply_skips_zone_that_refuses_transition() -> None:
    """One stuck zone (UPnP 701: empty queue or mid-transition) must
    not crash the whole batch. The skipped zone is dropped from both
    ``affected`` and ``skipped`` (the user didn't exclude it, and we
    didn't pause it either)."""

    good = _FakeSonosZone("RINCON_A", "A", is_playing=True)
    stuck = _FakeSonosZone(
        "RINCON_B",
        "B",
        is_playing=True,
        raise_transition_unavailable_on="pause",
    )
    state = _state(sonos_zones=[good, stuck])
    affected, skipped = await bulk_pause_sonos_apply(state)
    assert affected == ["RINCON_A"]
    assert skipped == []
    assert good.calls == ["pause"]
    assert stuck.calls == ["pause"]


@pytest.mark.asyncio
async def test_bulk_pause_sonos_apply_with_no_manager_returns_empty() -> None:
    state = _state(sonos_zones=None)
    affected, skipped = await bulk_pause_sonos_apply(state)
    assert affected == []
    assert skipped == []


@pytest.mark.asyncio
async def test_bulk_off_global_apply_pauses_sonos_alongside_kasa(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="sonos", canonical_key="RINCON_B", exclude_from_global=True
    )
    kasa = _FakeKasa("10.0.0.1", "Lamp", is_on=True)
    a = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    b = _FakeSonosZone("RINCON_B", "Office", is_playing=True)
    c = _FakeSonosZone("RINCON_C", "Patio", is_playing=False)
    state = _state(
        kasa_devices=[kasa], sonos_zones=[a, b, c], cache_path=db
    )
    affected, skipped = await bulk_off_global_apply(state, cache_path=db)
    assert ("kasa", "10.0.0.1") in affected
    assert ("sonos", "RINCON_A") in affected
    assert ("sonos", "RINCON_B") not in affected
    assert ("sonos", "RINCON_C") not in affected
    assert skipped == [("sonos", "RINCON_B")]
    assert a.calls == ["pause"]
    assert b.calls == []
    assert c.calls == []


def test_find_sonos_by_identifier_returns_match_or_none() -> None:
    zone = _FakeSonosZone("RINCON_AAAA", "Kitchen", is_playing=True)
    state = _state(sonos_zones=[zone])
    assert state.sonos_mgr is not None
    assert find_sonos_by_identifier(state.sonos_mgr, "RINCON_AAAA") is zone  # type: ignore[comparison-overlap]
    assert (
        find_sonos_by_identifier(state.sonos_mgr, "  RINCON_AAAA  ") is zone  # type: ignore[comparison-overlap]
    )
    assert find_sonos_by_identifier(state.sonos_mgr, "RINCON_ZZZZ") is None
    assert find_sonos_by_identifier(state.sonos_mgr, "") is None
    assert find_sonos_by_identifier(state.sonos_mgr, "   ") is None


def test_post_sonos_pause_all_only_pauses_playing_zones() -> None:
    a = _FakeSonosZone("RINCON_A", "A", is_playing=True)
    b = _FakeSonosZone("RINCON_B", "B", is_playing=False)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[a, b])
    runtime.discovery_error = None
    r = client.post("/v1/ui/sonos/pause-all")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"affected": ["RINCON_A"], "skipped": []}
    assert a.is_playing is False
    assert b.is_playing is False
    assert a.calls == ["pause"]
    assert b.calls == []


def test_post_sonos_pause_all_returns_empty_when_no_sonos_manager() -> None:
    client, app = _client()
    runtime.device_state = _state(sonos_zones=None)
    runtime.discovery_error = None
    r = client.post("/v1/ui/sonos/pause-all")
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {"affected": [], "skipped": []}


def test_post_sonos_toggle_pauses_zone_and_returns_refreshed_view(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    kasa_discovery_store.upsert_ui_preference(
        db, backend="sonos", canonical_key="RINCON_A", exclude_from_global=False
    )
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone], cache_path=db)
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"playing": False},
    )
    assert r.status_code == HTTPStatus.OK
    body = r.json()
    assert body["device"]["id"] == "RINCON_A"
    assert body["device"]["kind"] == "speaker"
    assert body["device"]["state"] == "paused"
    assert body["device"]["family_id"] == "sonos"
    assert body["device"]["exclude_from_global"] is False
    assert zone.is_playing is False
    assert zone.calls == ["pause"]


def test_post_sonos_toggle_resumes_zone() -> None:
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=False)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"playing": True},
    )
    assert r.status_code == HTTPStatus.OK
    assert r.json()["device"]["state"] == "playing"
    assert zone.is_playing is True
    assert zone.calls == ["resume:0"]


def test_post_sonos_toggle_returns_409_when_resume_hits_upnp_transition_error() -> None:
    """Reproduces the user-reported 500: clicking ``Resume it`` on a
    zone with an empty queue (or one that's mid-transition) raises
    UPnP 701 inside SoCo. The endpoint must translate that into a
    clean 409 Conflict so the front-end's existing failure path
    drops the optimistic prediction and refreshes — and the response
    body carries a human-readable hint."""

    zone = _FakeSonosZone(
        "RINCON_A",
        "Kitchen",
        is_playing=False,
        raise_transition_unavailable_on="resume",
    )
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"playing": True},
    )
    assert r.status_code == HTTPStatus.CONFLICT
    detail = r.json()["detail"]
    assert "Kitchen" in detail
    assert "cannot resume" in detail.lower()
    # The cache was already updated by the device-side handler before
    # the exception escaped, so a subsequent ``GET /v1/ui/state`` (or
    # the front-end's failure-path refresh) sees the right answer.
    assert zone.is_playing is False
    assert zone.calls == ["resume:0"]


def test_post_sonos_toggle_returns_409_when_pause_hits_upnp_transition_error() -> None:
    zone = _FakeSonosZone(
        "RINCON_A",
        "Office",
        is_playing=True,
        raise_transition_unavailable_on="pause",
    )
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"playing": False},
    )
    assert r.status_code == HTTPStatus.CONFLICT
    detail = r.json()["detail"]
    assert "Office" in detail
    assert "cannot pause" in detail.lower()
    assert zone.is_playing is False


def test_post_sonos_toggle_returns_404_for_unknown_zone() -> None:
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_ZZZZ/toggle",
        json={"playing": False},
    )
    assert r.status_code == HTTPStatus.NOT_FOUND
    assert "RINCON_ZZZZ" in r.json()["detail"]


def test_post_sonos_toggle_returns_404_when_no_sonos_manager() -> None:
    client, app = _client()
    runtime.device_state = _state(sonos_zones=None)
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"playing": False},
    )
    assert r.status_code == HTTPStatus.NOT_FOUND
    assert "Sonos" in r.json()["detail"]


def test_post_sonos_toggle_with_invalid_body_returns_422() -> None:
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone])
    runtime.discovery_error = None
    r = client.post(
        "/v1/ui/sonos/zones/RINCON_A/toggle",
        json={"on": False},
    )
    assert r.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_put_ui_preference_persists_sonos_exclusion(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone], cache_path=db)
    runtime.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/sonos/RINCON_A",
        json={"exclude_from_global": True},
    )
    assert r.status_code == HTTPStatus.OK
    assert r.json() == {
        "family_id": "sonos",
        "device_id": "RINCON_A",
        "exclude_from_global": True,
    }
    assert kasa_discovery_store.load_ui_preferences(db) == [
        ("sonos", "RINCON_A", True),
    ]


def test_put_ui_preference_returns_404_for_unknown_sonos_zone(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    zone = _FakeSonosZone("RINCON_A", "Kitchen", is_playing=True)
    client, app = _client()
    runtime.device_state = _state(sonos_zones=[zone], cache_path=db)
    runtime.discovery_error = None
    r = client.put(
        "/v1/ui/preferences/sonos/RINCON_ZZZZ",
        json={"exclude_from_global": True},
    )
    assert r.status_code == HTTPStatus.NOT_FOUND
    assert "RINCON_ZZZZ" in r.json()["detail"]


def test_sonos_endpoints_appear_in_openapi_schema() -> None:
    client, _app = _client()
    paths = client.get("/openapi.json").json().get("paths", {})
    assert "/v1/ui/sonos/pause-all" in paths
    assert "/v1/ui/sonos/zones/{device_id}/toggle" in paths
