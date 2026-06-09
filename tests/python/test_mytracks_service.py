"""Unit tests for My Tracks export client helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app.mytracks_service import (
    ExportedGeofence,
    ExportedParticipant,
    MyTracksSyncError,
    fetch_geofences_from_my_tracks,
    fetch_participants_from_my_tracks,
    normalize_mytracks_base_url,
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
        if path == "/api/admin/domesti/participants/":
            return httpx.Response(
                200,
                json=self._export_payload,
                request=MagicMock(),
            )
        if path == "/api/admin/domesti/geofences/":
            return httpx.Response(
                200,
                json=self._export_payload,
                request=MagicMock(),
            )
        raise AssertionError(f"Unexpected GET {path}")

    def post(self, path: str, **_kwargs: object) -> httpx.Response:
        assert path == "/login/"
        return httpx.Response(302, request=MagicMock())


def test_fetch_participants_from_my_tracks_parses_export_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "source": "my-tracks",
        "participants": [
            {
                "participant_id": "henrique",
                "display_name": "Henrique",
                "tracking_device_label": "Pixel",
                "enabled": True,
            },
        ],
    }
    monkeypatch.setattr(
        "app.mytracks_service._login_client",
        lambda *_args, **_kwargs: _FakeClient(export_payload=payload),
    )
    rows = fetch_participants_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert rows == [
        ExportedParticipant(
            participant_id="henrique",
            display_name="Henrique",
            tracking_device_label="Pixel",
            enabled=True,
        ),
    ]


def test_fetch_geofences_from_my_tracks_parses_export_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "source": "my-tracks",
        "geofences": [
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
