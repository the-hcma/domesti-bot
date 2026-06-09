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
from app.mytracks_store import load_mytracks_config


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


@patch("app.api.mytracks_routes.sync_participants_from_my_tracks", return_value=2)
def test_post_mytracks_participants_sync_records_timestamp(
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
        "/v1/rules/participants/sync",
        json={"username": "admin", "password": "secret"},
    )
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["participant_count"] == 2
    assert body["last_synced_at"] is not None


@patch("app.api.mytracks_routes.sync_geofences_from_my_tracks", return_value=3)
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
    assert body["geofence_count"] == 3
    assert body["last_synced_at"] is not None


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
        "/v1/rules/participants/sync",
        json={"username": "admin", "password": ""},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
