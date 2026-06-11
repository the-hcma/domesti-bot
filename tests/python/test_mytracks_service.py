"""Unit tests for My Tracks export client helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app.mytracks_service import (
    ExportedGeofence,
    ExportedUser,
    MyTracksSyncError,
    fetch_geofences_from_my_tracks,
    fetch_mytracks_domesti_config,
    fetch_users_from_my_tracks,
    normalize_mytracks_base_url,
    pair_with_my_tracks,
)


def test_normalize_mytracks_base_url_adds_https_scheme() -> None:
    assert normalize_mytracks_base_url("tracks.example.com") == "https://tracks.example.com"


def test_normalize_mytracks_base_url_rejects_empty_domain() -> None:
    with pytest.raises(MyTracksSyncError, match="Expected My Tracks domain"):
        normalize_mytracks_base_url("   ")


class _FakeClient:
    def __init__(self, *, export_payload: object) -> None:
        self._export_payload = export_payload
        self.cookies = httpx.Cookies()
        self.cookies.set("csrftoken", "csrf-token")
        self.cookies.set("sessionid", "session-token")

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def close(self) -> None:
        return None

    def get(self, path: str) -> httpx.Response:
        if path == "/login/":
            return httpx.Response(200, request=MagicMock())
        if path == "/api/admin/users-with-devices/":
            return httpx.Response(
                200,
                json=self._export_payload,
                headers={"content-type": "application/json"},
                request=MagicMock(),
            )
        if path == "/api/admin/waypoints/":
            return httpx.Response(
                200,
                json=self._export_payload,
                headers={"content-type": "application/json"},
                request=MagicMock(),
            )
        raise AssertionError(f"Unexpected GET {path}")

    def post(self, path: str, **_kwargs: object) -> httpx.Response:
        assert path == "/login/"
        return httpx.Response(302, request=MagicMock())


def test_fetch_users_from_my_tracks_parses_export_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "source": "my-tracks",
        "users_with_devices": [
            {
                "username": "henrique",
                "display_name": "Henrique",
                "device_name": "Pixel",
                "enabled": True,
            },
        ],
    }
    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _FakeClient(export_payload=payload),
    )
    rows = fetch_users_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert rows == [
        ExportedUser(
            user_id="henrique",
            first_name="Henrique",
            last_name="",
            display_name="Henrique",
            tracking_device_label="Pixel",
            enabled=True,
            latest_location=None,
        ),
    ]


def test_fetch_users_parses_latest_location(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "source": "my-tracks",
        "users_with_devices": [
            {
                "username": "henrique",
                "display_name": "Henrique Custodio",
                "device_name": "Pixel",
                "enabled": True,
                "latest_location": {
                    "lat": 41.194072,
                    "lon": -73.888325,
                    "accuracy_m": 12,
                    "timestamp": "2026-06-09T20:00:00+00:00",
                },
            },
        ],
    }
    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _FakeClient(export_payload=payload),
    )
    rows = fetch_users_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert len(rows) == 1
    location = rows[0].latest_location
    assert location is not None
    assert location.lat == 41.194072
    assert location.accuracy_m == 12
    assert location.received_at == "2026-06-09T20:00:00+00:00"


def test_fetch_geofences_from_my_tracks_parses_export_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "source": "my-tracks",
        "waypoints": [
            {
                "geofence_id": "henrique-house",
                "label": "House",
                "center_lat": 41.194072,
                "center_lon": -73.888325,
                "radius_m": 250,
                "enabled": True,
                "owntracks_rid": "rid-1",
            },
        ],
    }
    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _FakeClient(export_payload=payload),
    )
    rows = fetch_geofences_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert rows == [
        ExportedGeofence(
            geofence_id="henrique-house",
            label="House",
            center_lat=41.194072,
            center_lon=-73.888325,
            radius_m=250,
            enabled=True,
            owntracks_rid="rid-1",
        ),
    ]


def test_fetch_geofences_uses_login_client_without_context_manager_reentry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: httpx rejects `with client` after login GET/POST on the same instance."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login/" and request.method == "GET":
            return httpx.Response(
                200,
                headers={"set-cookie": "csrftoken=csrf-token; Path=/"},
                text='<input name="csrfmiddlewaretoken" value="csrf-token" />',
            )
        if request.url.path == "/login/" and request.method == "POST":
            return httpx.Response(
                302,
                headers={"set-cookie": "sessionid=session-token; Path=/"},
            )
        if request.url.path == "/api/admin/waypoints/":
            return httpx.Response(
                200,
                json={"waypoints": []},
                headers={"content-type": "application/json"},
            )
        raise AssertionError(f"Unexpected {request.method} {request.url.path}")

    transport = httpx.MockTransport(_handler)
    original_client = httpx.Client

    def _client_with_transport(*args: object, **kwargs: object) -> httpx.Client:
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client_with_transport)
    rows = fetch_geofences_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert rows == []


def test_fetch_mytracks_domesti_config_prefers_user_location_url_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConfigClient(_FakeClient):
        def get(self, path: str) -> httpx.Response:
            if path == "/api/admin/domesti-bot/config/":
                return httpx.Response(
                    200,
                    json={
                        "user_location_test_url": "https://domesti.example.com/test",
                        "user_location_update_url": "https://domesti.example.com/live",
                    },
                    headers={"content-type": "application/json"},
                    request=MagicMock(),
                )
            return super().get(path)

    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _ConfigClient(export_payload={}),
    )
    config = fetch_mytracks_domesti_config(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert config.user_location_update_url == "https://domesti.example.com/live"
    assert config.user_location_test_url == "https://domesti.example.com/test"


def test_fetch_mytracks_domesti_config_falls_back_to_legacy_pairing_url_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ConfigClient(_FakeClient):
        def get(self, path: str) -> httpx.Response:
            if path == "/api/admin/domesti-bot/config/":
                return httpx.Response(
                    200,
                    json={
                        "participant_location_test_url": "https://domesti.example.com/test",
                        "participant_location_update_url": "https://domesti.example.com/live",
                    },
                    headers={"content-type": "application/json"},
                    request=MagicMock(),
                )
            return super().get(path)

    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _ConfigClient(export_payload={}),
    )
    config = fetch_mytracks_domesti_config(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert config.user_location_update_url == "https://domesti.example.com/live"
    assert config.user_location_test_url == "https://domesti.example.com/test"


def test_pair_with_my_tracks_sends_user_location_url_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted_json: dict[str, object] = {}

    class _PairClient:
        cookies = httpx.Cookies()

        def __init__(self) -> None:
            self.cookies.set("csrftoken", "csrf-token")

        def close(self) -> None:
            return None

        def post(
            self,
            path: str,
            *,
            json: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
        ) -> httpx.Response:
            del headers
            if path == "/api/admin/domesti-bot/pair/":
                posted_json.update(json or {})
            return httpx.Response(200, request=MagicMock())

    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _PairClient(),
    )
    monkeypatch.setattr(
        "app.mytracks_service._session_csrf_token",
        lambda _client: "csrf-token",
    )
    status = pair_with_my_tracks(
        api_key="relay-key",
        base_url="https://tracks.example.com",
        domesti_base_url="https://domesti.example.com",
        user_location_test_url="https://domesti.example.com/v1/webhooks/location_update/test",
        user_location_update_url="https://domesti.example.com/v1/webhooks/location_update",
        password="secret",
        username="admin",
    )
    assert status == 200
    assert posted_json["user_location_update_url"] == (
        "https://domesti.example.com/v1/webhooks/location_update"
    )
    assert posted_json["user_location_test_url"] == (
        "https://domesti.example.com/v1/webhooks/location_update/test"
    )
    assert "participant_location_update_url" not in posted_json


def test_fetch_users_rejects_html_export_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _HtmlExportClient(_FakeClient):
        def get(self, path: str) -> httpx.Response:
            if path in {"/api/admin/users-with-devices/", "/api/admin/waypoints/"}:
                return httpx.Response(
                    200,
                    text="<html>login</html>",
                    headers={"content-type": "text/html"},
                    request=MagicMock(),
                )
            return super().get(path)

    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _HtmlExportClient(export_payload={}),
    )
    with pytest.raises(MyTracksSyncError, match="non-JSON"):
        fetch_users_from_my_tracks(
            base_url="https://tracks.example.com",
            username="admin",
            password="secret",
        )
