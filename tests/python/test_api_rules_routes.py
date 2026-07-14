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
        "away-garage-open-alert",
        "away-shutdown-everyone-outside-20m",
        "daylight-master-bedroom-fan-on-alert",
        "evening-arrival-home-lights",
        "evening-interior-lights-on-anyone-home",
        "evening-lights-off-both-home",
        "kristen-west-point-arrive",
        "kristen-west-point-leave",
        "morning-master-bedroom-fan-off",
    }
    assert ids == expected_ids

    one = client.get("/v1/rules/evening-arrival-home-lights")
    assert one.status_code == HTTPStatus.OK
    assert one.json()["label"] == "Evening arrival — front + garage lights"

    location = client.get("/v1/rules/settings/location")
    assert location.status_code == HTTPStatus.OK
    assert location.json()["timezone"] == "America/New_York"
    assert location.json()["home_configured"] is True

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


def test_put_user_home_wifi_and_list_observed_wifi(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
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
    from app.location_history_retention import default_location_history_retention
    from app.presence_store import UserLocationRecord, upsert_user_location

    upsert_user_location(
        db,
        UserLocationRecord(
            user_id="henrique",
            lat=41.0,
            lon=-73.0,
            accuracy_m=20,
            fix_at=100.0, reported_at=100.0,
            source="my-tracks",
            wifi_ssid="HomeNet",
            wifi_bssid="aa:bb:cc:dd:ee:ff",
        ),
        retention=default_location_history_retention(),
    )
    client = _client(db)
    observed = client.get("/v1/rules/users/henrique/observed-wifi")
    assert observed.status_code == HTTPStatus.OK
    assert observed.json()[0]["wifi_ssid"] == "HomeNet"
    assert observed.json()[0]["wifi_bssid"] == "aa:bb:cc:dd:ee:ff"

    put = client.put(
        "/v1/rules/users/henrique/home-wifi",
        json={"wifi_ssid": "HomeNet", "wifi_bssid": "aa:bb:cc:dd:ee:ff"},
    )
    assert put.status_code == HTTPStatus.OK
    assert put.json()["home_wifi_bssid"] == "aa:bb:cc:dd:ee:ff"


def test_put_user_home_wifi_rejects_partial_ssid_without_bssid(tmp_path: Path) -> None:
    db = tmp_path / "ui.sqlite"
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
    response = client.put(
        "/v1/rules/users/henrique/home-wifi",
        json={"wifi_ssid": "HomeNet", "wifi_bssid": "   "},
    )
    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


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


def test_put_rules_settings_location_updates_operator_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "automation-rules.json"
    path.write_text(_EXAMPLE_BUNDLE.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(path))
    client = _client(tmp_path / "unused.sqlite")

    put = client.put(
        "/v1/rules/settings/location",
        json={
            "lat": 41.2,
            "lon": -73.9,
            "timezone": "America/New_York",
            "home_label": "Updated Home",
        },
    )
    assert put.status_code == HTTPStatus.OK
    assert put.json()["home_label"] == "Updated Home"
    assert put.json()["home_configured"] is True
    assert put.json()["lat"] == 41.2

    listed = client.get("/v1/rules/settings/location")
    assert listed.status_code == HTTPStatus.OK
    assert listed.json()["home_label"] == "Updated Home"
