"""Unit tests for server-side automation rule condition evaluation."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.api.schemas import (
    AfterSunsetCondition,
    GeofenceOut,
    UserLocationOut,
    UsersInsideGeofenceCondition,
    RuleConditionsOut,
    RuleOut,
    SettingsLocationOut,
)
from app.rule_conditions import (
    RuleEvaluationContext,
    compute_rules_sun_out,
    evaluate_rule,
)
from app.rules_status import build_rules_status

_SETTINGS = SettingsLocationOut(
    home_label="Home",
    lat=41.194072,
    lon=-73.8883254,
    timezone="America/New_York",
)
_TZ = ZoneInfo("America/New_York")


def _evening_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                AfterSunsetCondition(
                    type="after_sunset",
                    offset_minutes=0,
                    window_end="midnight",
                ),
                UsersInsideGeofenceCondition(
                    type="users_inside_geofence",
                    geofence_id="house",
                    user_ids=["henrique"],
                ),
            ],
        ),
        cooldown_s=300,
        device_actions=[],
        enabled=True,
        id="evening-arrival-home-lights",
        label="Evening arrival",
        min_location_accuracy_m=50,
        notification_email=None,
        notify_on_fire=False,
        trigger="edge_true",
    )


def _ctx(
    *,
    now: datetime,
    geofences: tuple[GeofenceOut, ...] = (),
    user_locations: dict[str, UserLocationOut] | None = None,
) -> RuleEvaluationContext:
    sun = compute_rules_sun_out(_SETTINGS, now=now)
    return RuleEvaluationContext(
        geofences=geofences,
        now=now,
        user_display_names={"henrique": "Henrique"},
        user_locations=user_locations or {},
        sun=sun,
        timezone=_TZ,
    )


def test_after_sunset_met_in_evening_window() -> None:
    # June 9 2026 sunset at this lat/lon is ~8:27 PM local — use 9 PM.
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    result = evaluate_rule(_evening_rule(), _ctx(now=now))
    assert result.conditions[0].met is True
    assert "Evening window active" in result.conditions[0].detail


def test_after_sunset_not_met_midday() -> None:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=_TZ)
    result = evaluate_rule(_evening_rule(), _ctx(now=now))
    assert result.conditions[0].met is False
    assert "Outside sunset" in result.conditions[0].detail


def test_users_inside_geofence_met_with_fix() -> None:
    now = datetime(2026, 6, 9, 21, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    fix = UserLocationOut(
        accuracy_m=20,
        lat=41.1941,
        lon=-73.8883,
        received_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_rule(),
        _ctx(now=now, geofences=(geofence,), user_locations={"henrique": fix}),
    )
    assert result.conditions[1].met is True
    assert result.all_met is True


def test_users_inside_geofence_ignores_low_accuracy() -> None:
    now = datetime(2026, 6, 9, 20, 0, tzinfo=_TZ)
    geofence = GeofenceOut(
        center_lat=41.194072,
        center_lon=-73.888325,
        enabled=True,
        geofence_id="house",
        label="House",
        owntracks_rid=None,
        radius_m=250,
    )
    fix = UserLocationOut(
        accuracy_m=120,
        lat=41.1941,
        lon=-73.8883,
        received_at="2026-06-09T23:00:00Z",
        source="owntracks",
    )
    result = evaluate_rule(
        _evening_rule(),
        _ctx(now=now, geofences=(geofence,), user_locations={"henrique": fix}),
    )
    assert result.conditions[1].met is False
    assert "Ignored low-accuracy location" in result.conditions[1].detail


def test_build_rules_status_from_example_bundle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    example = repo_root / "automation-rules.json.example"
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(example))
    status = build_rules_status(cache_path=tmp_path / "unused.sqlite")
    assert status.using_mock is False
    assert len(status.rules) == 3
    assert status.sun.sunset_at.endswith("Z")
    assert status.evaluator.last_run_at is not None
