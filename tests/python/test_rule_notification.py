"""Hermetic tests for rule notification email copy and deep links."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.api.schemas import RuleConditionsOut, RuleOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.mytracks_store import MyTracksPairingSave, save_mytracks_pairing
from app.rule_device_action_outcome import RuleDeviceActionOutcome
from app.rule_notification import (
    build_rule_notification_bodies,
    domesti_public_base_url,
    format_device_action_outcomes,
    rule_automation_status_url,
)


def _sample_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[]),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="away-shutdown",
        label="Away shutdown",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        trigger="edge_true",
    )


def test_domesti_public_base_url_prefers_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com/")
    assert domesti_public_base_url(tmp_path / "cache.sqlite") == "https://domesti.example.com"


def test_domesti_public_base_url_reads_mytracks_pair_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_PUBLIC_BASE_URL", raising=False)
    cache_path = tmp_path / "cache.sqlite"
    save_mytracks_pairing(
        cache_path,
        MyTracksPairingSave(
            domain="https://tracks.example.com",
            domesti_public_base_url="https://home.example.com",
            user_location_test_url="https://home.example.com/v1/webhooks/location_update/test",
            user_location_update_url="https://home.example.com/v1/webhooks/location_update",
            username="henrique",
        ),
    )
    assert domesti_public_base_url(cache_path) == "https://home.example.com"


def test_rule_automation_status_url_builds_status_deep_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    assert (
        rule_automation_status_url(tmp_path / "cache.sqlite", "away-shutdown")
        == "https://domesti.example.com/#/automations/status/away-shutdown"
    )


def test_format_device_action_outcomes_includes_before_and_after() -> None:
    outcomes = (
        RuleDeviceActionOutcome(
            action=RuleDeviceActionType.TURN_OFF,
            after_state="off",
            before_state="on",
            device_id="Garage lights",
            error=None,
            family_id=DeviceFamilyId.KASA,
            probable=False,
            succeeded=True,
        ),
    )
    assert format_device_action_outcomes(outcomes) == (
        "Garage lights (Kasa): on → off",
    )


def test_build_rule_notification_bodies_includes_device_states_and_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_PUBLIC_BASE_URL", "https://domesti.example.com")
    rule = _sample_rule()
    outcomes = (
        RuleDeviceActionOutcome(
            action=RuleDeviceActionType.TURN_OFF,
            after_state="off",
            before_state="on",
            device_id="Kitchen TV",
            error=None,
            family_id=DeviceFamilyId.VIZIO,
            probable=False,
            succeeded=True,
        ),
    )
    plain, html = build_rule_notification_bodies(
        rule,
        cache_path=tmp_path / "cache.sqlite",
        device_action_outcomes=outcomes,
        notification_detail="Everyone left home.",
    )
    assert "Kitchen TV (Vizio): on → off" in plain
    assert "Everyone left home." in plain
    assert (
        "https://domesti.example.com/#/automations/status/away-shutdown" in plain
    )
    assert "Kitchen TV (Vizio): on → off" in html
    assert (
        'href="https://domesti.example.com/#/automations/status/away-shutdown"'
        in html
    )
    assert "Open Automations → Status" not in plain


def test_build_rule_notification_bodies_falls_back_without_public_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_PUBLIC_BASE_URL", raising=False)
    plain, html = build_rule_notification_bodies(
        _sample_rule(),
        cache_path=tmp_path / "cache.sqlite",
    )
    assert "Open Automations → Status in domesti-bot" in plain
    assert "View live status:" not in plain
    assert "href=" not in html
