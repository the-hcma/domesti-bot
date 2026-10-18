"""Device presence notices in ``GET /v1/ui/state`` and the three ``/v1/ui/presence`` actions (domesti-bot#867)."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from unittest.mock import AsyncMock, MagicMock, PropertyMock

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.device_display import format_device_display
from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence import device_presence_store
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
