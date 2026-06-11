"""Tests for my-tracks location-update webhooks and pairing routes."""

from __future__ import annotations

import argparse
import logging
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.db.secrets import (
    load_mytracks_relay_api_key_from_db,
    save_mytracks_relay_api_key_to_db,
)
from app.mytracks_store import load_mytracks_pair_status
from app.presence_store import list_user_locations
from app.rules_store import UserRecord, replace_users

_LOCATION_UPDATE_PAYLOAD = {
    "user_id": "henrique",
    "lat": 41.194085,
    "lon": -73.888365,
    "accuracy_m": 12,
    "timestamp": "2026-06-09T23:14:58+00:00",
    "source": "my-tracks",
}


@pytest.fixture
def fernet_key(monkeypatch: pytest.MonkeyPatch) -> str:
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", key)
    return key


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def _seed_user(db: Path) -> None:
    replace_users(
        db,
        [
            UserRecord(
                user_id="henrique",
                first_name="Test",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
            ),
        ],
    )


def _store_relay_key(db: Path, relay_key: str, fernet_key: str) -> None:
    _ = fernet_key
    save_mytracks_relay_api_key_to_db(db, relay_key)


def test_location_update_webhook_rejects_missing_relay_key(
    tmp_path: Path, fernet_key: str
) -> None:
    _ = fernet_key
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": "missing"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_location_update_webhook_rejects_env_api_key_instead_of_relay_key(
    tmp_path: Path,
    fernet_key: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "ui.sqlite"
    monkeypatch.setenv("DOMESTI_API_KEY", "operator-key")
    client, _app = _client(cache_path=db)
    _seed_user(db)
    _store_relay_key(db, "relay-secret", fernet_key)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": "operator-key"},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_location_update_webhook_rejects_legacy_participant_id_field(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    legacy_payload = {
        key: value
        for key, value in _LOCATION_UPDATE_PAYLOAD.items()
        if key != "user_id"
    }
    legacy_payload["participant_id"] = "henrique"
    response = client.post(
        "/v1/webhooks/location_update",
        json=legacy_payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_location_update_webhook_stores_location_for_known_user(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    locations = list_user_locations(db)
    assert locations["henrique"].lat == 41.194085


def test_location_update_webhook_returns_404_for_unknown_user(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NOT_FOUND


def test_location_update_test_webhook_logs_without_location_prefix(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _ListHandler()
    location_logger = logging.getLogger("location")
    old_handlers = list(location_logger.handlers)
    old_level = location_logger.level
    old_propagate = location_logger.propagate
    location_logger.handlers.clear()
    location_logger.addHandler(handler)
    location_logger.setLevel(logging.INFO)
    location_logger.propagate = False
    try:
        response = client.post(
            "/v1/webhooks/location_update/test",
            json=_LOCATION_UPDATE_PAYLOAD,
            headers={"X-Domesti-Api-Key": relay_key},
        )
    finally:
        location_logger.removeHandler(handler)
        location_logger.handlers = old_handlers
        location_logger.setLevel(old_level)
        location_logger.propagate = old_propagate
    assert response.status_code == HTTPStatus.NO_CONTENT
    messages = [record.getMessage() for record in records]
    assert messages == ["test webhook accepted for henrique (discarded)"]
    assert "[location]" not in messages[0]


def test_location_update_test_webhook_does_not_persist_location(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    response = client.post(
        "/v1/webhooks/location_update/test",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list_user_locations(db) == {}


def test_location_update_test_webhook_accepts_unknown_user(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    response = client.post(
        "/v1/webhooks/location_update/test",
        json={**_LOCATION_UPDATE_PAYLOAD, "user_id": "house_meister"},
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list_user_locations(db) == {}


@patch("app.api.mytracks_routes.pair_with_my_tracks")
def test_post_mytracks_pair_persists_relay_key_and_status(
    pair_mock: object,
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    response = client.post(
        "/v1/settings/my-tracks/pair",
        json={
            "domain": "https://tracks.example.com",
            "username": "admin",
            "password": "secret",
        },
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["relay_key_configured"] is True
    assert body["paired_at"] is not None
    assert body["location_history_retention"] == {
        "max_age_hours": 24.0,
        "min_keep_count": 20,
        "unlimited": False,
    }
    assert body["user_location_update_url"] == (
        "http://testserver/v1/webhooks/location_update"
    )
    assert body["user_location_test_url"] == (
        "http://testserver/v1/webhooks/location_update/test"
    )
    stored_key = load_mytracks_relay_api_key_from_db(db)
    assert stored_key is not None
    assert stored_key != ""
    status = load_mytracks_pair_status(db)
    assert status is not None
    assert status.paired_at is not None


def test_patch_location_updates_returns_dedicated_response(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks"):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    response = client.patch(
        "/v1/settings/my-tracks/location-updates",
        json={"accepted": False},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "accepted": False,
        "mytracks_location_updates_enabled": None,
    }


def test_location_update_webhook_returns_503_when_emergency_switch_off(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks"):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    relay_key = load_mytracks_relay_api_key_from_db(db)
    assert relay_key is not None
    client.patch(
        "/v1/settings/my-tracks/location-updates",
        json={"accepted": False},
    )
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.SERVICE_UNAVAILABLE
    assert response.headers.get("retry-after") == "60"


def test_patch_location_history_retention_updates_policy(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=200):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    response = client.patch(
        "/v1/settings/my-tracks/location-history-retention",
        json={"unlimited": True, "max_age_hours": 12.0, "min_keep_count": 5},
    )
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "max_age_hours": 12.0,
        "min_keep_count": 5,
        "unlimited": True,
    }


def test_location_update_webhook_appends_history_rows(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    from app.presence_store import count_user_location_history

    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    for lat in (41.1, 41.2):
        payload = {**_LOCATION_UPDATE_PAYLOAD, "lat": lat}
        response = client.post(
            "/v1/webhooks/location_update",
            json=payload,
            headers={"X-Domesti-Api-Key": relay_key},
        )
        assert response.status_code == HTTPStatus.NO_CONTENT
    assert count_user_location_history(db, "henrique") == 2


def test_location_update_test_webhook_works_when_emergency_switch_off(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks"):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    client.patch(
        "/v1/settings/my-tracks/location-updates",
        json={"accepted": False},
    )
    response = client.post(
        "/v1/webhooks/location_update/test",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": load_mytracks_relay_api_key_from_db(db) or ""},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT


def test_post_mytracks_pair_uses_forwarded_public_url(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=200):
        response = client.post(
            "/v1/settings/my-tracks/pair",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "domesti.example.com",
            },
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["domesti_public_base_url"] == "https://domesti.example.com"
    assert body["user_location_update_url"] == (
        "https://domesti.example.com/v1/webhooks/location_update"
    )


def test_get_mytracks_relay_key_returns_stored_secret(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=200):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    response = client.get("/v1/settings/my-tracks/relay-key")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["configured"] is True
    stored = load_mytracks_relay_api_key_from_db(db)
    assert stored is not None
    assert body["stored_relay_key"] == stored


def test_delete_mytracks_pair_clears_relay_key(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=200):
        client.post(
            "/v1/settings/my-tracks/pair",
            json={
                "domain": "https://tracks.example.com",
                "username": "admin",
                "password": "secret",
            },
        )
    assert load_mytracks_relay_api_key_from_db(db) is not None
    response = client.delete("/v1/settings/my-tracks/pair")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["paired_at"] is None
    assert body["relay_key_configured"] is False
    assert load_mytracks_relay_api_key_from_db(db) is None
    relay_response = client.get("/v1/settings/my-tracks/relay-key")
    assert relay_response.json() == {
        "configured": False,
        "stored_relay_key": None,
    }
