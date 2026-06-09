"""Tests for persisted Automations participant and geofence HTTP routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path

from fastapi.testclient import TestClient

from app.api.app import create_app
from app.rules_store import GeofenceRecord, ParticipantRecord, replace_geofences, replace_participants


def _client(cache_path: Path) -> TestClient:
    args = argparse.Namespace(
        discovery_cache=str(cache_path),
        tailwind_token=None,
    )
    return TestClient(create_app(args))


def test_get_geofences_and_participants(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    replace_geofences(
        db,
        [
            GeofenceRecord(
                geofence_id="henrique-house",
                label="House",
                center_lat=41.194072,
                center_lon=-73.888325,
                radius_m=250,
                enabled=True,
                owntracks_rid=None,
            ),
        ],
    )
    replace_participants(
        db,
        [
            ParticipantRecord(
                participant_id="henrique",
                display_name="Henrique",
                tracking_device_label="Pixel",
                enabled=True,
            ),
        ],
    )
    client = _client(db)

    geofences = client.get("/v1/rules/geofences")
    assert geofences.status_code == HTTPStatus.OK
    assert geofences.json()[0]["geofence_id"] == "henrique-house"

    participants = client.get("/v1/rules/participants")
    assert participants.status_code == HTTPStatus.OK
    assert participants.json()[0]["participant_id"] == "henrique"


def test_put_and_delete_geofence(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
    client = _client(db)
    payload = {
        "geofence_id": "office",
        "label": "Office",
        "center_lat": 40.0,
        "center_lon": -74.0,
        "radius_m": 100,
        "enabled": True,
        "owntracks_rid": None,
    }
    put = client.put("/v1/rules/geofences/office", json=payload)
    assert put.status_code == HTTPStatus.OK
    assert put.json()["label"] == "Office"

    delete = client.delete("/v1/rules/geofences/office")
    assert delete.status_code == HTTPStatus.NO_CONTENT
    listed = client.get("/v1/rules/geofences")
    assert listed.json() == []
