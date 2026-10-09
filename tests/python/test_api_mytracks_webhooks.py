"""Tests for my-tracks location-update webhooks and pairing routes."""

from __future__ import annotations

import argparse
import itertools
import logging
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.api_key_compare import api_keys_match
from app.api.app import create_app
from app.db.secrets import (
    load_mytracks_relay_api_key_from_db,
    save_mytracks_relay_api_key_to_db,
)
from app.mytracks_service import MyTracksPairResult, MyTracksSyncError
from app.mytracks_store import load_mytracks_pair_status
from app.presence_store import list_user_locations
from app.rules_store import UserRecord, replace_users

_PAIR_OK = MyTracksPairResult(status_code=HTTPStatus.OK)

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


def test_location_update_webhook_rejects_missing_relay_key(tmp_path: Path, fernet_key: str) -> None:
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


def test_location_update_webhook_checks_relay_key_with_the_constant_time_helper(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    with patch("app.api.mytracks_relay_auth.api_keys_match", wraps=api_keys_match) as match:
        response = client.post(
            "/v1/webhooks/location_update",
            json=_LOCATION_UPDATE_PAYLOAD,
            headers={"X-Domesti-Api-Key": relay_key},
        )
    assert response.status_code == HTTPStatus.NO_CONTENT
    match.assert_called_once_with(relay_key, relay_key)


@pytest.mark.parametrize("presented", ["relay-secret-valuX", "", "   "])
def test_location_update_webhook_rejects_wrong_same_length_and_blank_keys(
    tmp_path: Path,
    fernet_key: str,
    presented: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    _store_relay_key(db, "relay-secret-value", fernet_key)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers={"X-Domesti-Api-Key": presented},
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_location_update_webhook_rejects_non_ascii_key_without_error(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    _store_relay_key(db, "relay-secret-value", fernet_key)
    response = client.post(
        "/v1/webhooks/location_update",
        json=_LOCATION_UPDATE_PAYLOAD,
        headers=[(b"X-Domesti-Api-Key", "caf\u00e9".encode("latin-1"))],
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_location_update_webhook_requires_user_id_field(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload_without_user_id = {key: value for key, value in _LOCATION_UPDATE_PAYLOAD.items() if key != "user_id"}
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload_without_user_id,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_location_update_webhook_rejects_blank_user_id(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload = {**_LOCATION_UPDATE_PAYLOAD, "user_id": "   "}
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload,
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


def test_location_update_webhook_ping_with_old_fix_updates_location(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    """A ping whose GPS fix predates the report still updates last location."""
    from app.location_report import parse_iso_timestamp_to_epoch

    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    fresh_payload = {
        **_LOCATION_UPDATE_PAYLOAD,
        "timestamp": "2026-06-30T12:09:00+00:00",
        "reported_at": "2026-06-30T12:09:00+00:00",
        "lat": 41.1,
    }
    response = client.post(
        "/v1/webhooks/location_update",
        json=fresh_payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    ping_payload = {
        **_LOCATION_UPDATE_PAYLOAD,
        "timestamp": "2026-06-30T10:00:00+00:00",
        "reported_at": "2026-06-30T14:01:00+00:00",
        "trigger": "p",
        "lat": 41.2,
    }
    response = client.post(
        "/v1/webhooks/location_update",
        json=ping_payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    stored = list_user_locations(db)["henrique"]
    assert stored.lat == 41.2
    assert stored.reported_at == parse_iso_timestamp_to_epoch("2026-06-30T14:01:00+00:00")
    assert stored.fix_at == parse_iso_timestamp_to_epoch("2026-06-30T10:00:00+00:00")


def test_location_update_webhook_drops_stale_report(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    """Older report times do not replace a newer stored location."""
    from app.location_report import parse_iso_timestamp_to_epoch

    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    newer_payload = {
        **_LOCATION_UPDATE_PAYLOAD,
        "timestamp": "2026-06-30T14:01:00+00:00",
        "reported_at": "2026-06-30T14:01:00+00:00",
        "lat": 41.2,
    }
    response = client.post(
        "/v1/webhooks/location_update",
        json=newer_payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    stale_payload = {
        **_LOCATION_UPDATE_PAYLOAD,
        "timestamp": "2026-06-30T15:00:00+00:00",
        "reported_at": "2026-06-30T12:00:00+00:00",
        "lat": 41.9,
    }
    response = client.post(
        "/v1/webhooks/location_update",
        json=stale_payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    stored = list_user_locations(db)["henrique"]
    assert stored.lat == 41.2
    assert stored.reported_at == parse_iso_timestamp_to_epoch("2026-06-30T14:01:00+00:00")


def test_location_update_webhook_stores_connection_type(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload = {**_LOCATION_UPDATE_PAYLOAD, "connection_type": "w"}
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list_user_locations(db)["henrique"].connection_type == "w"


def test_location_update_webhook_stores_wifi_metadata(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload = {
        **_LOCATION_UPDATE_PAYLOAD,
        "connection_type": "w",
        "wifi_ssid": "HCMA-Home",
        "wifi_bssid": "DE:AD:BE:EF:00:01",
        "fix_source": "w",
        "trigger": "p",
        "battery_level": 77,
    }
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    stored = list_user_locations(db)["henrique"]
    assert stored.wifi_ssid == "HCMA-Home"
    assert stored.wifi_bssid == "de:ad:be:ef:00:01"
    assert stored.fix_source == "w"
    assert stored.trigger == "p"
    assert stored.battery_level == 77


def test_location_update_webhook_canonicalizes_connection_type(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload = {**_LOCATION_UPDATE_PAYLOAD, "connection_type": "W"}
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.NO_CONTENT
    assert list_user_locations(db)["henrique"].connection_type == "w"


def test_location_update_webhook_rejects_invalid_connection_type(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    _seed_user(db)
    relay_key = "relay-secret-value"
    _store_relay_key(db, relay_key, fernet_key)
    payload = {**_LOCATION_UPDATE_PAYLOAD, "connection_type": "x"}
    response = client.post(
        "/v1/webhooks/location_update",
        json=payload,
        headers={"X-Domesti-Api-Key": relay_key},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


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


@patch(
    "app.api.mytracks_routes.pair_with_my_tracks",
    return_value=_PAIR_OK,
)
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
    assert body["user_location_update_url"] == ("http://testserver/v1/webhooks/location_update")
    assert body["user_location_test_url"] == ("http://testserver/v1/webhooks/location_update/test")
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
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    assert body["user_location_update_url"] == ("https://domesti.example.com/v1/webhooks/location_update")


_PAIR_BODY = {
    "domain": "https://tracks.example.com",
    "username": "admin",
    "password": "secret",
}


def test_relay_key_is_never_returned_by_the_pair_response_or_pair_status(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        paired = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    stored = load_mytracks_relay_api_key_from_db(db)
    assert stored is not None
    assert paired.status_code == HTTPStatus.OK
    status = client.get("/v1/settings/my-tracks/pair-status")
    for response in (paired, status):
        assert stored not in response.text
        body = response.json()
        assert body["relay_key_configured"] is True
        assert isinstance(body["relay_key_updated_at"], float)
        assert "stored_relay_key" not in body
        assert "relay_key" not in body


def test_the_relay_key_readback_endpoint_is_gone(tmp_path: Path, fernet_key: str) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert client.get("/v1/settings/my-tracks/relay-key").status_code == HTTPStatus.NOT_FOUND


def test_re_pairing_replaces_the_relay_key_and_moves_updated_at(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    times = itertools.count(100.0, 300.0)
    monkeypatch.setattr("app.db.secrets.time.time", lambda: next(times))
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        first = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
        first_key = load_mytracks_relay_api_key_from_db(db)
        second = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    second_key = load_mytracks_relay_api_key_from_db(db)
    assert first_key is not None
    assert second_key is not None
    assert first_key != second_key
    assert first.json()["relay_key_updated_at"] < second.json()["relay_key_updated_at"]


def test_pair_status_is_null_before_any_pairing(tmp_path: Path, fernet_key: str) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    assert client.get("/v1/settings/my-tracks/pair-status").json() is None


def test_a_failed_first_pair_stores_no_relay_key_and_reports_the_error(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", side_effect=MyTracksSyncError("rejected")):
        failed = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert failed.status_code == HTTPStatus.BAD_GATEWAY
    assert load_mytracks_relay_api_key_from_db(db) is None
    status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert status["relay_key_configured"] is False
    assert status["relay_key_updated_at"] is None
    assert status["paired_at"] is None
    assert status["last_pair_error"] == "rejected"


def test_a_failed_re_pair_keeps_the_previous_working_relay_key(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    times = itertools.count(100.0, 300.0)
    monkeypatch.setattr("app.db.secrets.time.time", lambda: next(times))
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        first = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    registered_key = load_mytracks_relay_api_key_from_db(db)
    assert first.status_code == HTTPStatus.OK
    with patch("app.api.mytracks_routes.pair_with_my_tracks", side_effect=MyTracksSyncError("bad password")):
        failed = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert failed.status_code == HTTPStatus.BAD_GATEWAY
    assert load_mytracks_relay_api_key_from_db(db) == registered_key
    status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert status["relay_key_updated_at"] == first.json()["relay_key_updated_at"]
    assert status["paired_at"] is not None


def test_a_storage_failure_after_my_tracks_accepted_the_key_is_recorded_as_a_mismatch(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        first = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert first.status_code == HTTPStatus.OK
    old_key = load_mytracks_relay_api_key_from_db(db)
    with (
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK),
        patch(
            "app.api.mytracks_routes.adopt_legacy_shared_key",
            side_effect=OSError("disk full"),
        ),
    ):
        failed = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert failed.status_code == HTTPStatus.INTERNAL_SERVER_ERROR
    assert "webhooks fail until you pair again" in failed.json()["detail"]
    assert load_mytracks_relay_api_key_from_db(db) == old_key
    status = client.get("/v1/settings/my-tracks/pair-status").json()
    assert status["paired_at"] is not None
    assert "accepted a new relay key" in status["last_pair_error"]
    assert "disk full" not in status["last_pair_error"]


def test_a_successful_pair_clears_a_recorded_mismatch(tmp_path: Path, fernet_key: str) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    with (
        patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK),
        patch("app.api.mytracks_routes.adopt_legacy_shared_key", side_effect=OSError("disk full")),
    ):
        client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
        repaired = client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert repaired.status_code == HTTPStatus.OK
    assert repaired.json()["last_pair_error"] is None


def test_the_key_is_registered_with_my_tracks_before_it_is_stored(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    seen_at_registration: list[str | None] = []

    def _pair(**kwargs: str) -> MyTracksPairResult:
        seen_at_registration.append(load_mytracks_relay_api_key_from_db(db))
        assert kwargs["api_key"]
        return _PAIR_OK

    with patch("app.api.mytracks_routes.pair_with_my_tracks", side_effect=_pair):
        client.post("/v1/settings/my-tracks/pair", json=_PAIR_BODY)
    assert seen_at_registration == [None]
    assert load_mytracks_relay_api_key_from_db(db) is not None


def test_delete_mytracks_pair_clears_relay_key(
    tmp_path: Path,
    fernet_key: str,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    with patch("app.api.mytracks_routes.pair_with_my_tracks", return_value=_PAIR_OK):
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
    assert body["relay_key_updated_at"] is None
