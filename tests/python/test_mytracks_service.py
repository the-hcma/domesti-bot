"""Unit tests for My Tracks export client helpers."""

from __future__ import annotations

import pytest

from app.mytracks_service import (
    MyTracksSyncError,
    normalize_mytracks_base_url,
    sync_participants_from_my_tracks,
)


def test_normalize_mytracks_base_url_adds_https_scheme() -> None:
    assert normalize_mytracks_base_url("tracks.example.com") == "https://tracks.example.com"


def test_normalize_mytracks_base_url_rejects_empty_domain() -> None:
    with pytest.raises(MyTracksSyncError, match="Expected My Tracks domain"):
        normalize_mytracks_base_url("   ")


def test_sync_participants_from_my_tracks_counts_list_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 200

        @staticmethod
        def json() -> list[dict[str, str]]:
            return [{"participant_id": "a"}, {"participant_id": "b"}]

    monkeypatch.setattr(
        "app.mytracks_service.httpx.get",
        lambda *args, **kwargs: FakeResponse(),
    )
    count = sync_participants_from_my_tracks(
        base_url="https://tracks.example.com",
        username="admin",
        password="secret",
    )
    assert count == 2
