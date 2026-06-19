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
    assert len(bundle.rules) == 5
    assert bundle.rules[0].id == "evening-arrival-home-lights"
    lights_off = next(
        rule for rule in bundle.rules if rule.id == "evening-lights-off-both-home"
    )
    assert lights_off.trigger == "scheduled"
    assert lights_off.schedule_cron == "*/15 * * * *"
    interior = next(
        rule
        for rule in bundle.rules
        if rule.id == "evening-interior-lights-on-anyone-home"
    )
    assert interior.fire_once_per_local_day is True
    assert automation_rules_source() == "operator"


def test_list_automation_rules_returns_all_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    example = Path(__file__).resolve().parents[2] / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    rules = list_automation_rules()
    assert {rule.id for rule in rules} == {
        "evening-arrival-home-lights",
        "evening-interior-lights-on-anyone-home",
        "evening-lights-off-both-home",
        "kristen-west-point-arrive",
        "kristen-west-point-leave",
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
