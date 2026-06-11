"""Tests for My Tracks settings and sync routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.mytracks_service import ExportedGeofence, ExportedUser, ExportedUserLocation
from app.mytracks_store import load_mytracks_config
from app.presence_store import list_user_locations
from app.rules_store import list_geofences, list_users


def _client(*, cache_path: Path | None) -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace(
        discovery_cache=str(cache_path) if cache_path is not None else None,
        tailwind_token=None,
    )
    app = create_app(args)
    return TestClient(app), app


def test_get_mytracks_settings_returns_null_when_unconfigured(tmp_path: Path) -> None:
    client, _app = _client(cache_path=tmp_path / "ui.sqlite")
    response = client.get("/v1/settings/my-tracks")
    assert response.status_code == HTTPStatus.OK
    assert response.json() is None


def test_put_mytracks_settings_persists_config(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    payload = {
        "domain": "https://tracks.example.com",
        "username": "admin",
    }
    response = client.put("/v1/settings/my-tracks", json=payload)
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body == {
        "domain": "https://tracks.example.com",
        "username": "admin",
    }

    saved = load_mytracks_config(db)
    assert saved is not None
    assert saved.domain == "https://tracks.example.com"
    assert saved.username == "admin"


@patch(
    "app.api.mytracks_routes.fetch_users_from_my_tracks",
    return_value=[
        ExportedUser(
            user_id="henrique",
            first_name="Henrique",
            last_name="",
            display_name="Henrique",
            tracking_device_label="Pixel",
            enabled=True,
            latest_location=ExportedUserLocation(
                lat=41.194072,
                lon=-73.888325,
                accuracy_m=12,
                received_at="2026-06-09T20:00:00+00:00",
            ),
        ),
    ],
)
def test_post_mytracks_users_sync_records_timestamp(
    _sync_mock: object,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/settings/my-tracks",
        json={
            "domain": "https://tracks.example.com",
            "username": "admin",
        },
    )
    response = client.post(
        "/v1/rules/users/sync",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["user_count"] == 1
    assert body["last_synced_at"] is not None
    assert body["source"] == "my-tracks"

    users = list_users(db)
    assert len(users) == 1
    assert users[0].user_id == "henrique"
    locations = list_user_locations(db)
    assert "henrique" in locations
    assert locations["henrique"].lat == 41.194072


def test_get_users_status_returns_synced_locations(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/settings/my-tracks",
        json={
            "domain": "https://tracks.example.com",
            "username": "admin",
        },
    )
    with patch(
        "app.api.mytracks_routes.fetch_users_from_my_tracks",
        return_value=[
            ExportedUser(
                user_id="henrique",
                first_name="Henrique",
                last_name="",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
                latest_location=ExportedUserLocation(
                    lat=41.194072,
                    lon=-73.888325,
                    accuracy_m=12,
                    received_at="2026-06-09T20:00:00+00:00",
                ),
            ),
        ],
    ):
        sync = client.post(
            "/v1/rules/users/sync",
            json={"username": "admin", "password": "secret"},
        )
    assert sync.status_code == HTTPStatus.OK
    response = client.get("/v1/rules/users/status")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert len(body) == 1
    assert body[0]["last_location"]["lat"] == 41.194072
    assert body[0]["age_seconds"] is not None


@patch(
    "app.api.mytracks_routes.fetch_geofences_from_my_tracks",
    return_value=[
        ExportedGeofence(
            geofence_id="henrique-house",
            label="House",
            center_lat=41.194072,
            center_lon=-73.888325,
            radius_m=250,
            enabled=True,
            owntracks_rid="rid-1",
        ),
    ],
)
def test_post_mytracks_geofences_sync_records_timestamp(
    _sync_mock: object,
    tmp_path: Path,
) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/settings/my-tracks",
        json={
            "domain": "https://tracks.example.com",
            "username": "admin",
        },
    )
    response = client.post(
        "/v1/rules/geofences/sync",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["geofence_count"] == 1
    assert body["last_synced_at"] is not None
    assert body["source"] == "my-tracks"

    geofences = list_geofences(db)
    assert len(geofences) == 1
    assert geofences[0].geofence_id == "henrique-house"


def test_post_mytracks_sync_rejects_empty_password(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client, _app = _client(cache_path=db)
    client.put(
        "/v1/settings/my-tracks",
        json={
            "domain": "https://tracks.example.com",
            "username": "admin",
        },
    )
    response = client.post(
        "/v1/rules/users/sync",
        json={"username": "admin", "password": ""},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
