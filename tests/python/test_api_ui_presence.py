"""Device presence notices in ``GET /v1/ui/state`` and the three ``/v1/ui/presence`` actions (domesti-bot#867)."""

from __future__ import annotations

import argparse
import asyncio
import threading
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.device_display import format_device_display
from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence import device_presence_store
from app.device_presence_decision_store import list_keep_checking, record_keep_checking
from app.domesti_bot_cli import DeviceManagersState
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import runtime

_MAC = "5c:e9:31:60:c5:bd"
_LABEL = "Master bedroom fan"


def _client(*, forget_returns: bool = True) -> tuple[TestClient, MagicMock]:
    kasa = MagicMock(spec=KasaDeviceManager)
    type(kasa).switches = PropertyMock(return_value=())
    kasa.forget_device = AsyncMock(return_value=forget_returns)
    client = TestClient(create_app(argparse.Namespace()))
    runtime.device_state = DeviceManagersState(
        kasa_mgr=kasa,
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=None,
        vizio_mgr=None,
        cache_path=None,
        args=argparse.Namespace(),
    )
    runtime.discovery_error = None
    return client, kasa


def _missing_fan() -> str:
    notice = device_presence_store.note_disappeared(
        device_id=_MAC, family_id="kasa", host="192.168.86.167", label=_LABEL
    )
    assert notice is not None
    return notice.notice_id


def _returned_fan() -> str:
    _missing_fan()
    notice = device_presence_store.note_reappeared(
        device_id=_MAC, family_id="kasa", host="192.168.86.167", label=_LABEL
    )
    assert notice is not None
    return notice.notice_id


def test_ui_state_lists_a_disappeared_device_with_a_human_message() -> None:
    client, _kasa = _client()
    notice_id = _missing_fan()
    body = client.get("/v1/ui/state").json()
    (notice,) = body["presence_notices"]
    assert notice["notice_id"] == notice_id
    assert notice["kind"] == DevicePresenceKind.DISAPPEARED
    assert (notice["family_id"], notice["device_id"], notice["label"]) == ("kasa", _MAC, _LABEL)
    assert format_device_display(_MAC, _LABEL) in notice["message"]
    assert "stopped responding" in notice["message"]


def test_ui_state_lists_a_returned_device() -> None:
    client, _kasa = _client()
    _returned_fan()
    (notice,) = client.get("/v1/ui/state").json()["presence_notices"]
    assert notice["kind"] == DevicePresenceKind.REAPPEARED
    assert "responding again" in notice["message"]


def test_ui_state_has_no_notices_by_default() -> None:
    client, _kasa = _client()
    assert client.get("/v1/ui/state").json()["presence_notices"] == []


def test_keep_checking_hides_the_notice_but_keeps_tracking_the_device() -> None:
    client, kasa = _client()
    notice_id = _missing_fan()
    response = client.post(f"/v1/ui/presence/{notice_id}/keep-checking")
    assert response.status_code == HTTPStatus.OK
    assert client.get("/v1/ui/state").json()["presence_notices"] == []
    stored = device_presence_store.get(notice_id)
    assert stored is not None and stored.decision == DevicePresenceDecision.KEEP_CHECKING
    kasa.forget_device.assert_not_awaited()


def test_forget_removes_the_device_and_the_notice() -> None:
    client, kasa = _client()
    notice_id = _missing_fan()
    response = client.post(f"/v1/ui/presence/{notice_id}/forget")
    assert response.status_code == HTTPStatus.OK
    assert response.json()["device_id"] == _MAC
    kasa.forget_device.assert_awaited_once_with(_MAC)
    assert client.get("/v1/ui/state").json()["presence_notices"] == []
    assert device_presence_store.get(notice_id) is None


def test_forget_of_a_device_the_manager_no_longer_knows_is_404_and_clears_the_notice() -> None:
    client, _kasa = _client(forget_returns=False)
    notice_id = _missing_fan()
    response = client.post(f"/v1/ui/presence/{notice_id}/forget")
    assert response.status_code == HTTPStatus.NOT_FOUND
    assert client.get("/v1/ui/state").json()["presence_notices"] == []


def test_dismiss_clears_a_returned_notice() -> None:
    client, _kasa = _client()
    notice_id = _returned_fan()
    assert client.post(f"/v1/ui/presence/{notice_id}/dismiss").status_code == HTTPStatus.OK
    assert client.get("/v1/ui/state").json()["presence_notices"] == []


def test_actions_on_an_unknown_notice_are_404() -> None:
    client, _kasa = _client()
    for action in ("keep-checking", "forget", "dismiss"):
        response = client.post(f"/v1/ui/presence/nope/{action}")
        assert response.status_code == HTTPStatus.NOT_FOUND, action


def test_keep_checking_and_forget_refuse_a_returned_notice() -> None:
    client, kasa = _client()
    notice_id = _returned_fan()
    for action in ("keep-checking", "forget"):
        response = client.post(f"/v1/ui/presence/{notice_id}/{action}")
        assert response.status_code == HTTPStatus.CONFLICT, action
    kasa.forget_device.assert_not_awaited()


def test_dismiss_refuses_a_disappeared_notice() -> None:
    client, _kasa = _client()
    notice_id = _missing_fan()
    assert client.post(f"/v1/ui/presence/{notice_id}/dismiss").status_code == HTTPStatus.CONFLICT


def test_forget_that_fails_in_the_manager_keeps_the_notice() -> None:
    client, kasa = _client()
    client = TestClient(client.app, raise_server_exceptions=False)
    kasa.forget_device = AsyncMock(side_effect=RuntimeError("database is locked"))
    notice_id = _missing_fan()
    response = client.post(f"/v1/ui/presence/{notice_id}/forget")
    assert response.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert [n["notice_id"] for n in client.get("/v1/ui/state").json()["presence_notices"]] == [notice_id]


def test_lifespan_attaches_the_presence_store_to_the_discovery_database_before_discovery(tmp_path: Path) -> None:
    cache_path = tmp_path / "discovery.sqlite"
    record_keep_checking(cache_path, "kasa", _MAC)
    attached_when_discovery_starts: list[Path | None] = []

    async def _bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        attached_when_discovery_starts.append(device_presence_store.database_path)
        while True:
            await asyncio.sleep(1.0)

    app = create_app(argparse.Namespace(discovery_cache=str(cache_path)))
    with patch("app.api.app.bootstrap_device_managers", _bootstrap), TestClient(app) as client:
        assert client.get("/health").status_code == HTTPStatus.OK
        deadline = time.monotonic() + 10.0
        while not attached_when_discovery_starts and time.monotonic() < deadline:
            time.sleep(0.02)
        assert attached_when_discovery_starts == [cache_path.resolve()]
        # The stored answer was loaded: the same plug going missing now stays hidden.
        notice = device_presence_store.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label=_LABEL)
        assert notice is not None and notice.decision == DevicePresenceDecision.KEEP_CHECKING


def test_keep_checking_is_stored_in_the_discovery_database(tmp_path: Path) -> None:
    cache_path = tmp_path / "discovery.sqlite"
    client, _kasa = _client()
    device_presence_store.attach(cache_path)
    notice_id = _missing_fan()
    response = client.post(f"/v1/ui/presence/{notice_id}/keep-checking")
    assert response.status_code == HTTPStatus.OK
    device_presence_store.flush()
    assert set(list_keep_checking(cache_path)) == {("kasa", _MAC)}


def test_a_slow_discovery_database_does_not_delay_http_startup(tmp_path: Path) -> None:
    """Attaching the store happens in the discovery task, so a locked database cannot stall /health."""

    release_attach = threading.Event()

    def _slow_attach(_path: Path | None) -> None:
        release_attach.wait(timeout=10)

    async def _bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        while True:
            await asyncio.sleep(1.0)

    app = create_app(argparse.Namespace(discovery_cache=str(tmp_path / "discovery.sqlite")))
    with (
        patch("app.api.app.bootstrap_device_managers", _bootstrap),
        patch.object(device_presence_store, "attach", _slow_attach),
    ):
        started = time.perf_counter()
        with TestClient(app) as client:
            assert time.perf_counter() - started < 5.0
            assert client.get("/health").status_code == HTTPStatus.OK
            release_attach.set()


def test_shutdown_flushes_a_keep_checking_answer_that_was_still_queued(tmp_path: Path) -> None:
    cache_path = tmp_path / "discovery.sqlite"
    attached = threading.Event()

    async def _bootstrap(*_args: Any, **_kwargs: Any) -> Any:
        attached.set()
        while True:
            await asyncio.sleep(1.0)

    app = create_app(argparse.Namespace(discovery_cache=str(cache_path)))
    with patch("app.api.app.bootstrap_device_managers", _bootstrap), TestClient(app) as client:
        assert client.get("/health").status_code == HTTPStatus.OK
        assert attached.wait(timeout=10)
        device_presence_store.keep_checking(_missing_fan())
    # The writer queue was drained on the way out, so the answer is in the database.
    assert set(list_keep_checking(cache_path)) == {("kasa", _MAC)}


def test_forget_for_a_family_whose_manager_cannot_forget_yet_is_409_and_keeps_the_notice() -> None:
    client, kasa = _client()
    notice = device_presence_store.note_disappeared(
        device_id="RINCON_AAAAAAAAAAAA01400", family_id="sonos", host=None, label="Living Room"
    )
    assert notice is not None
    response = client.post(f"/v1/ui/presence/{notice.notice_id}/forget")
    assert response.status_code == HTTPStatus.CONFLICT
    assert "sonos" in response.json()["detail"]
    kasa.forget_device.assert_not_awaited()
    assert [n["notice_id"] for n in client.get("/v1/ui/state").json()["presence_notices"]] == [notice.notice_id]


def test_forget_is_dispatched_to_the_manager_of_the_notices_family() -> None:
    client, kasa = _client()
    sonos = MagicMock()
    sonos.forget_device = AsyncMock(return_value=True)
    runtime.device_state = runtime.device_state._replace(sonos_mgr=sonos)  # type: ignore[union-attr]
    notice = device_presence_store.note_disappeared(
        device_id="aa:aa:aa:aa:aa:aa", family_id="sonos", host=None, label="Living Room"
    )
    assert notice is not None
    response = client.post(f"/v1/ui/presence/{notice.notice_id}/forget")
    assert response.status_code == HTTPStatus.OK
    sonos.forget_device.assert_awaited_once_with("aa:aa:aa:aa:aa:aa")
    kasa.forget_device.assert_not_awaited()
    assert client.get("/v1/ui/state").json()["presence_notices"] == []
