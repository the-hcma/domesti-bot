"""Tests for ``automation-rules.json`` loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.automation_rules_loader import (
    AutomationRulesLoadError,
    automation_rules_source,
    list_automation_rules,
    load_automation_rules_bundle,
    load_settings_location,
)


def test_load_example_bundle_from_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    bundle = load_automation_rules_bundle()
    assert bundle.version == 1
    assert len(bundle.rules) == 13
    assert bundle.rules[0].id == "evening-arrival-home-lights"
    lights_off = next(rule for rule in bundle.rules if rule.id == "evening-lights-off-both-home")
    assert lights_off.triggers == ["scheduled"]
    assert lights_off.schedule_cron == "*/10 * * * *"
    interior = next(rule for rule in bundle.rules if rule.id == "evening-interior-lights-on-anyone-home")
    assert interior.fire_once_per_local_day is True
    assert interior.triggers == ["edge_true", "scheduled"]
    assert interior.schedule_cron is None
    ep1_alert = next(rule for rule in bundle.rules if rule.id == "office-ep1-occupied-alert")
    assert ep1_alert.enabled is False
    assert ep1_alert.device_actions == []
    occupied = ep1_alert.conditions.all[0]
    assert occupied.type == "devices_any_in_state"
    assert occupied.state == "occupied"
    assert occupied.devices[0].family_id == "ep1"
    ep1_hot = next(rule for rule in bundle.rules if rule.id == "office-ep1-hot-alert")
    assert ep1_hot.enabled is False
    hot = ep1_hot.conditions.all[0]
    assert hot.type == "ep1_reading_compare"
    assert hot.comparison == "above"
    assert hot.metric == "temperature_c"
    assert hot.threshold == 24.0
    dark_lights = next(rule for rule in bundle.rules if rule.id == "daylight-dark-house-lights-on")
    assert dark_lights.enabled is False
    assert dark_lights.schedule_cron == "*/5 * * * *"
    assert dark_lights.conditions.all[0].type == "daylight"
    lux = dark_lights.conditions.all[1]
    assert lux.type == "ep1_reading_compare"
    assert lux.comparison == "below"
    assert lux.metric == "illuminance_lx"
    assert lux.threshold == 80.0
    assert lux.device.display_name == "Window EP1"
    assert dark_lights.device_actions[0].action == "turn_on"
    power_cycle = next(rule for rule in bundle.rules if rule.id == "hdhomerun-nightly-power-cycle")
    assert power_cycle.enabled is False
    assert power_cycle.device_actions[1].delay_s == 60
    assert automation_rules_source() == "operator"


def test_list_automation_rules_returns_all_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    rules = list_automation_rules()
    assert {rule.id for rule in rules} == {
        "away-garage-open-alert",
        "away-shutdown-everyone-outside-20m",
        "daylight-dark-house-lights-on",
        "daylight-master-bedroom-fan-on-alert",
        "evening-arrival-home-lights",
        "evening-interior-lights-on-anyone-home",
        "evening-lights-off-both-home",
        "hdhomerun-nightly-power-cycle",
        "kristen-west-point-arrive",
        "kristen-west-point-leave",
        "morning-master-bedroom-fan-off",
        "office-ep1-hot-alert",
        "office-ep1-occupied-alert",
    }


def test_load_settings_location_from_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    location = load_settings_location()
    assert location.timezone == "America/New_York"
    assert location.home_label == "Home"


def test_load_automation_rules_bundle_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "automation-rules.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AutomationRulesLoadError, match="invalid JSON"):
        load_automation_rules_bundle(path=path)


def test_load_automation_rules_bundle_rejects_invalid_schema(tmp_path: Path) -> None:
    path = tmp_path / "automation-rules.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings_location": {
                    "lat": 1.0,
                    "lon": 2.0,
                    "timezone": "UTC",
                },
                "rules": [{"id": "broken"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AutomationRulesLoadError, match="automation rules schema"):
        load_automation_rules_bundle(path=path)
