"""Unit tests for the configured home location reference."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.schemas import SettingsLocationIn, SettingsLocationOut
from app.automation_rules_loader import load_home_location, save_settings_location
from app.home_location import (
    HomeLocationNotConfiguredError,
    home_coordinates_configured,
    home_location_configured,
    resolve_home_location,
    try_resolve_home_location,
)


def test_home_coordinates_configured_rejects_zero_sentinel() -> None:
    assert home_coordinates_configured(lat=0.0, lon=0.0) is False
    assert home_coordinates_configured(lat=41.2, lon=0.0) is True
    assert home_coordinates_configured(lat=0.0, lon=-73.9) is True


def test_load_home_location_from_example_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    home = load_home_location()
    assert home.home_label == "Home"
    assert home.timezone == "America/New_York"
    assert home_coordinates_configured(lat=home.lat, lon=home.lon) is True


def test_load_home_location_raises_when_bundle_home_is_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "automation-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_id_resolution": "preferred_label",
                "settings_location": {
                    "lat": 0.0,
                    "lon": 0.0,
                    "timezone": "UTC",
                    "home_label": "Nowhere",
                },
                "rules": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(path))
    with pytest.raises(HomeLocationNotConfiguredError, match="unconfigured sentinel"):
        load_home_location()


def test_resolve_home_location_happy_path() -> None:
    settings = SettingsLocationOut(
        home_label="  Home  ",
        lat=41.194072,
        lon=-73.8883254,
        timezone="America/New_York",
    )
    assert home_location_configured(settings) is True
    assert settings.home_configured is True
    home = resolve_home_location(settings)
    assert home.lat == settings.lat
    assert home.lon == settings.lon
    assert home.timezone == "America/New_York"
    assert home.home_label == "Home"


def test_resolve_home_location_rejects_unconfigured_sentinel() -> None:
    settings = SettingsLocationOut(
        home_label="Unset",
        lat=0.0,
        lon=0.0,
        timezone="UTC",
    )
    assert home_location_configured(settings) is False
    assert settings.home_configured is False
    with pytest.raises(HomeLocationNotConfiguredError, match="unconfigured sentinel"):
        resolve_home_location(settings)
    assert try_resolve_home_location(settings) is None


def test_save_settings_location_updates_operator_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "automation-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_id_resolution": "preferred_label",
                "_notes": ["keep me"],
                "settings_location": {
                    "lat": 0.0,
                    "lon": 0.0,
                    "timezone": "UTC",
                    "home_label": "Unset",
                },
                "rules": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(path))
    saved = save_settings_location(
        SettingsLocationIn(
            home_label="Home",
            lat=41.19,
            lon=-73.88,
            timezone="America/New_York",
        ),
    )
    assert saved.home_configured is True
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["_notes"] == ["keep me"]
    assert raw["settings_location"]["lat"] == 41.19
    assert "home_configured" not in raw["settings_location"]
    assert load_home_location().home_label == "Home"
