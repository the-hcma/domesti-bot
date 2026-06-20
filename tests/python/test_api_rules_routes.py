"""Tests for persisted Automations user and geofence HTTP routes."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.rules_store import GeofenceRecord, UserRecord, replace_geofences, replace_users

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXAMPLE_BUNDLE = _REPO_ROOT / "automation-rules.json.example"


def _client(cache_path: Path) -> TestClient:
    args = argparse.Namespace(
        discovery_cache=str(cache_path),
        tailwind_token=None,
    )
    return TestClient(create_app(args))


def test_get_geofences_and_users(tmp_path: Path) -> None:
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
    client = _client(db)

    geofences = client.get("/v1/rules/geofences")
    assert geofences.status_code == HTTPStatus.OK
    assert geofences.json()[0]["geofence_id"] == "henrique-house"

    users = client.get("/v1/rules/users")
    assert users.status_code == HTTPStatus.OK
    assert users.json()[0]["user_id"] == "henrique"


def test_get_rules_from_file_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(_EXAMPLE_BUNDLE))
    client = _client(Path("/tmp/unused-rules-bundle.sqlite"))

    listed = client.get("/v1/rules")
    assert listed.status_code == HTTPStatus.OK
    ids = {row["id"] for row in listed.json()}
    expected_ids = {
        "away-pause-media",
        "evening-arrival-home-lights",
        "evening-interior-lights-on-anyone-home",
        "evening-lights-off-both-home",
        "kristen-west-point-arrive",
        "kristen-west-point-leave",
    }
    assert ids == expected_ids

    one = client.get("/v1/rules/evening-arrival-home-lights")
    assert one.status_code == HTTPStatus.OK
    assert one.json()["label"] == "Evening arrival — front + garage lights"

    location = client.get("/v1/rules/settings/location")
    assert location.status_code == HTTPStatus.OK
    assert location.json()["timezone"] == "America/New_York"

    status = client.get("/v1/rules/status")
    assert status.status_code == HTTPStatus.OK
    body = status.json()
    assert {row["id"] for row in body["rules"]} == expected_ids
    assert body["sun"]["sunset_at"].endswith("Z")


def test_get_rules_validation_route_before_rule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(_EXAMPLE_BUNDLE))
    client = _client(Path("/tmp/unused-rules-validation.sqlite"))

    validation = client.get("/v1/rules/validation")
    assert validation.status_code == HTTPStatus.OK
    body = validation.json()
    assert "rules" in body
    # Example bundle references henrique/kristen but the empty cache has no roster rows.
    assert len(body["rules"]) >= 1


def test_get_rules_status_route_before_rule_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(_EXAMPLE_BUNDLE))
    client = _client(Path("/tmp/unused-rules-status.sqlite"))

    status = client.get("/v1/rules/status")
    assert status.status_code == HTTPStatus.OK

    missing = client.get("/v1/rules/not-a-real-rule-id")
    assert missing.status_code == HTTPStatus.NOT_FOUND


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
