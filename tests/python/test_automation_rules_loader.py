"""Tests for ``automation-rules.json`` loading."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.automation_rules_loader import (
    AutomationRulesLoadError,
    automation_rules_json_path,
    automation_rules_operator_json_path,
    automation_rules_source,
    automation_rules_xdg_json_path,
    list_automation_rules,
    load_automation_rules_bundle,
    load_settings_location,
)


def test_load_example_bundle_from_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    bundle = load_automation_rules_bundle()
    assert bundle.version == 1
    assert len(bundle.rules) == 17
    assert bundle.rules[0].id == "evening-arrival-home-lights"
    lights_off = next(rule for rule in bundle.rules if rule.id == "evening-lights-off-both-home")
    assert lights_off.triggers == ["edge_true"]
    assert lights_off.schedule_cron is None
    interior = next(rule for rule in bundle.rules if rule.id == "evening-interior-lights-on-anyone-home")
    assert interior.fire_once_per_local_day is True
    assert interior.triggers == ["edge_true", "scheduled"]
    assert interior.schedule_cron is None
    garage_rules = [rule for rule in bundle.rules if rule.id.startswith("garage-arrival-")]
    assert len(garage_rules) == 3
    alone = [rule for rule in garage_rules if "-alone-" in rule.id]
    assert [getattr(rule.conditions.all[0], "max_inside_s", None) for rule in alone] == [300, 300]
    assert all(rule.dry_run and not rule.enabled for rule in garage_rules)
    assert all(
        [action.action for action in rule.device_actions] == ["open"] and len(rule.device_actions) == 1
        for rule in garage_rules
    )
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
    assert dark_lights.triggers == ["device_state"]
    assert dark_lights.schedule_cron is None
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
    stairs = next(rule for rule in bundle.rules if rule.id == "basement-stairs-companions")
    assert stairs.enabled is False
    assert stairs.triggers == ["device_state"]
    assert stairs.ignore_expected_device_changes is True
    assert stairs.conditions.all[0].type == "devices_any_transitioned_to_state"
    assert stairs.conditions.all[1].type == "local_time_window"
    assert [action.action for action in stairs.device_actions] == ["turn_on", "turn_on"]
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
        "basement-stairs-companions",
        "daylight-dark-house-lights-on",
        "daylight-master-bedroom-fan-on-alert",
        "evening-arrival-home-lights",
        "evening-interior-lights-on-anyone-home",
        "evening-lights-off-both-home",
        "garage-arrival-alone-open-henriques-side",
        "garage-arrival-alone-open-kristens-side",
        "garage-arrival-together-open-kristens-side",
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


def test_automation_rules_env_override_wins_over_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    xdg_dir = tmp_path / "xdg-config" / "domesti-bot"
    xdg_dir.mkdir(parents=True)
    (xdg_dir / "automation-rules.json").write_text("{}", encoding="utf-8")
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))

    assert automation_rules_json_path() == example.resolve()
    assert automation_rules_operator_json_path() == example.resolve()
    assert automation_rules_source() == "operator"


def test_automation_rules_example_path_ignores_domesti_bot_config_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_AUTOMATION_RULES_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))
    monkeypatch.setenv(
        "DOMESTI_BOT_CONFIG_FILE",
        str(tmp_path / "elsewhere" / "domesti-bot.config.json"),
    )

    resolved = automation_rules_json_path()
    assert resolved.name == "automation-rules.json.example"
    assert resolved.is_file()
    assert automation_rules_source() == "example"


def test_automation_rules_paths_fall_back_to_example_without_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_AUTOMATION_RULES_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "empty-xdg"))

    assert not automation_rules_xdg_json_path().is_file()
    resolved = automation_rules_json_path()
    assert resolved.name == "automation-rules.json.example"
    assert resolved.is_file()
    assert automation_rules_operator_json_path() == automation_rules_xdg_json_path().resolve()
    assert automation_rules_source() == "example"


def test_automation_rules_paths_prefer_xdg_operator_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_AUTOMATION_RULES_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    xdg_dir = tmp_path / "xdg-config" / "domesti-bot"
    xdg_dir.mkdir(parents=True)
    operator = xdg_dir / "automation-rules.json"
    operator.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")

    assert automation_rules_xdg_json_path() == operator
    assert automation_rules_json_path() == operator.resolve()
    assert automation_rules_operator_json_path() == operator.resolve()
    assert automation_rules_source() == "operator"
    assert load_automation_rules_bundle().version == 1


def test_bundle_loads_a_device_turn_on_rule_written_as_plain_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rule = {
        "id": "basement-stairs-companions",
        "label": "Basement stairs companions",
        "enabled": True,
        "triggers": ["device_state"],
        "cooldown_s": 0,
        "min_location_accuracy_m": 50,
        "ignore_expected_device_changes": True,
        "notify_on_fire": True,
        "notification_emails": ["ops@example.com"],
        "conditions": {
            "all": [
                {
                    "type": "devices_any_transitioned_to_state",
                    "state": "on",
                    "devices": [
                        {"family_id": "kasa", "device_id": "02:00:00:00:00:09", "display_name": "Stairs light"},
                    ],
                },
                {"type": "local_time_window", "start_hhmm": "06:30", "end_hhmm": "09:30"},
            ],
        },
        "device_actions": [
            {
                "family_id": "kasa",
                "device_id": "02:00:00:00:00:0a",
                "display_name": "Basement lamp",
                "action": "turn_on",
            },
        ],
    }
    bundle_path = tmp_path / "rules.json"
    bundle_path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_id_resolution": "mac",
                "settings_location": {
                    "home_label": "Home",
                    "lat": 41.194072,
                    "lon": -73.8883254,
                    "timezone": "America/New_York",
                },
                "rules": [rule],
            },
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle_path))
    loaded = load_automation_rules_bundle().rules[0]
    assert [condition.type for condition in loaded.conditions.all] == [
        "devices_any_transitioned_to_state",
        "local_time_window",
    ]
    assert loaded.ignore_expected_device_changes is True
    assert [action.action for action in loaded.device_actions] == ["turn_on"]
