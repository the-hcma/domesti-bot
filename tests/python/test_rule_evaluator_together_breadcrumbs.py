"""The evaluator leaves one log line when a tracked pair begins travelling together and one when it ends."""

from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

import pytest

from app import rule_conditions, rule_evaluator
from app.api.schemas import RuleConditionsOut, RuleOut, UsersTogetherCondition
from app.device_enums import RuleTrigger
from app.location_history_retention import default_location_history_retention
from app.presence_store import UserLocationRecord, upsert_user_location
from app.rule_evaluator import RuleEvaluator
from app.rules_store import UserRecord, replace_users

_T0 = 1_800_000_000.0
_M_PER_DEG = 2 * math.pi * 6_371_000.0 / 360.0
_LAT0 = 41.0
_LON0 = -73.9


def _rule(*, enabled: bool = True, user_ids: tuple[str, ...] = ("user-a", "user-b")) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=[UsersTogetherCondition(type="users_together", user_ids=list(user_ids))]),
        cooldown_s=0,
        device_actions=[],
        enabled=enabled,
        id="travelling-together",
        label="Travelling together",
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=[RuleTrigger.EDGE_TRUE],
    )


class _Rig:
    """An evaluator with two users and one ``users_together`` rule, fed positions on a simulated clock."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, rule: RuleOut) -> None:
        bundle = tmp_path / "rules.json"
        bundle.write_text(
            json.dumps(
                {
                    "version": 1,
                    "device_id_resolution": "mac",
                    "settings_location": {
                        "home_label": "Home",
                        "lat": _LAT0,
                        "lon": _LON0,
                        "timezone": "America/New_York",
                    },
                    "rules": [rule.model_dump(mode="json")],
                },
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
        self.db = tmp_path / "discovery.sqlite"
        self.clock = {"now": _T0}
        replace_users(
            self.db,
            [
                UserRecord(
                    user_id=user_id,
                    first_name=user_id,
                    last_name="",
                    display_name=user_id.upper(),
                    tracking_device_label="Phone",
                    enabled=True,
                )
                for user_id in ("user-a", "user-b", "user-c")
            ],
        )
        self.evaluator = RuleEvaluator(
            cache_path=self.db,
            device_state_getter=lambda: None,
            now_fn=lambda: self.clock["now"],
        )

    async def report(self, user_id: str, at: float, *, lateral_m: float) -> None:
        self.clock["now"] = at
        lat = _LAT0 + 20.0 * (at - _T0) / _M_PER_DEG
        lon = _LON0 + lateral_m / (_M_PER_DEG * math.cos(math.radians(_LAT0)))
        upsert_user_location(
            self.db,
            UserLocationRecord(
                user_id=user_id,
                lat=lat,
                lon=lon,
                accuracy_m=5,
                fix_at=at,
                reported_at=at,
                source="test",
            ),
            retention=default_location_history_retention(),
        )
        await self.evaluator.on_location_update(user_id)

    async def drive(self, *, until_s: float, split_at_s: float | None) -> None:
        """User A reports every 10 s and user B every 19 s; B drives 400 m away after ``split_at_s``."""
        reports = [(_T0 + 10.0 * index, "user-a") for index in range(int(until_s // 10) + 1)]
        reports += [(_T0 + 3.0 + 19.0 * index, "user-b") for index in range(int(until_s // 19) + 1)]
        for at, user_id in sorted(reports):
            apart = split_at_s is not None and user_id == "user-b" and at - _T0 > split_at_s
            await self.report(user_id, at, lateral_m=400.0 if apart else 3.0 if user_id == "user-b" else 0.0)


def _lines(caplog: pytest.LogCaptureFixture, word: str) -> list[str]:
    return [record.getMessage() for record in caplog.records if f"[rules] together {word}" in record.getMessage()]


@pytest.mark.asyncio
async def test_a_pair_travelling_together_and_then_apart_leaves_one_begin_and_one_end_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule())
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.drive(until_s=900.0, split_at_s=700.0)

    began, ended = _lines(caplog, "began"), _lines(caplog, "ended")
    assert len(began) == 1
    assert len(ended) == 1
    assert "users=user-a,user-b names='USER-A and USER-B'" in began[0]
    assert "rules=travelling-together" in began[0]
    assert "first_sight=true" in began[0]
    match = re.search(r"duration_s=(\d+)", ended[0])
    assert match is not None
    duration = int(match.group(1))
    assert 650 <= duration <= 760
    assert "reason=clearly_apart" in ended[0]
    positions = [record.getMessage() for record in caplog.records]
    assert positions.index(began[0]) < positions.index(ended[0])


@pytest.mark.asyncio
async def test_a_pair_that_stays_together_leaves_a_single_begin_line_and_no_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule())
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.drive(until_s=400.0, split_at_s=None)

    assert len(_lines(caplog, "began")) == 1
    assert _lines(caplog, "ended") == []


@pytest.mark.asyncio
async def test_a_disabled_rule_tracks_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule(enabled=False))
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.drive(until_s=400.0, split_at_s=None)

    assert _lines(caplog, "began") == []
    assert _lines(caplog, "ended") == []


@pytest.mark.asyncio
async def test_names_follow_the_order_of_the_user_ids_in_the_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule(user_ids=("user-b", "user-a")))
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.drive(until_s=400.0, split_at_s=None)

    (began,) = _lines(caplog, "began")
    assert "users=user-a,user-b names='USER-A and USER-B'" in began


@pytest.mark.asyncio
async def test_a_report_from_a_user_outside_the_pair_estimates_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule())
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.drive(until_s=400.0, split_at_s=None)
        calls: list[Any] = []
        original = rule_conditions.estimate_together

        def counting(*args: Any, **kwargs: Any) -> Any:
            calls.append(args)
            return original(*args, **kwargs)

        monkeypatch.setattr(rule_conditions, "estimate_together", counting)
        await rig.report("user-c", _T0 + 410.0, lateral_m=900.0)
        assert calls == []
        await rig.report("user-a", _T0 + 420.0, lateral_m=0.0)

    assert len(calls) >= 1
    assert len(_lines(caplog, "began")) == 1


@pytest.mark.asyncio
async def test_a_failure_while_tracking_is_logged_and_does_not_stop_the_rule_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    rig = _Rig(tmp_path, monkeypatch, rule=_rule())

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("Expected a pair, got nothing")

    monkeypatch.setattr(rule_evaluator, "users_together_pairs", boom)
    with caplog.at_level(logging.INFO, logger="app.rule_evaluator"):
        await rig.report("user-a", _T0 + 10.0, lateral_m=0.0)

    assert any("[rules] together tracking failed" in record.getMessage() for record in caplog.records)
