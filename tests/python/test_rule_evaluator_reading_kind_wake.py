"""Hermetic tests for EP1 reading-kind wake filtering (#672)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest

from app.api.schemas import (
    DevicesAnyInStateCondition,
    Ep1ReadingCompareCondition,
    RuleConditionDeviceRefOut,
    RuleConditionsOut,
    RuleOut,
)
from app.device_enums import (
    DeviceConditionState,
    DeviceFamilyId,
    Ep1ReadingComparison,
    Ep1ReadingMetric,
    RuleTrigger,
)
from app.domesti_bot_cli import DeviceManagersState
from app.ep1_device_manager import Ep1DeviceManager
from app.kasa_device_manager import KasaDeviceManager
from app.rule_actions import RuleNotificationEmailOutcome
from app.rule_evaluator import RuleEvaluator

_MAC = "aa:bb:cc:dd:ee:01"


class _FakeEp1:
    def __init__(
        self,
        identifier: str,
        label: str,
        *,
        illuminance_lx: float | None = None,
        occupancy_state: str | None = None,
    ) -> None:
        self.identifier = identifier
        self.mac_address = identifier
        self.preferred_label = label
        self.host = "192.0.2.10"
        self.port = 6053
        self.illuminance_lx = illuminance_lx
        self.humidity_pct = None
        self.temperature_c = None
        self.occupancy_state = occupancy_state
        self.unresponsive = False


class _ReadingKindFixture(TypedDict):
    device: _FakeEp1
    evaluator: RuleEvaluator


@pytest.mark.asyncio
async def test_illuminance_wake_does_not_evaluate_occupancy_only_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _reading_kind_fixture(
        tmp_path,
        monkeypatch,
        include_lux=False,
        include_occupancy=True,
    )
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await fixture["evaluator"].on_device_state_change(
            DeviceFamilyId.EP1,
            _MAC,
            reading_metric=Ep1ReadingMetric.ILLUMINANCE_LX,
        )
        assert send_mock.call_count == 0


@pytest.mark.asyncio
async def test_occupancy_bool_wake_does_not_evaluate_lux_only_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _reading_kind_fixture(
        tmp_path,
        monkeypatch,
        include_lux=True,
        include_occupancy=False,
    )
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await fixture["evaluator"].on_device_state_change(
            DeviceFamilyId.EP1,
            _MAC,
        )
        assert send_mock.call_count == 0


@pytest.mark.asyncio
async def test_illuminance_wake_evaluates_lux_rule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _reading_kind_fixture(
        tmp_path,
        monkeypatch,
        include_lux=True,
        include_occupancy=False,
    )
    with patch(
        "app.rule_evaluator.send_rule_notification_email",
        return_value=RuleNotificationEmailOutcome.sent_to(["ops@example.com"]),
    ) as send_mock:
        await fixture["evaluator"].on_device_state_change(
            DeviceFamilyId.EP1,
            _MAC,
            reading_metric=Ep1ReadingMetric.ILLUMINANCE_LX,
        )
        assert send_mock.call_count == 1
        send_kwargs = send_mock.call_args.kwargs
        # Metric wakes must not borrow the occupancy bool streak for Timing (#705).
        assert send_kwargs["device_state_changed_at"] is None
        assert send_kwargs["noticed_at"] == 1_700_000_000.0
        assert send_kwargs["fire_source"] == "device_state"


def _ep1_mgr(device: _FakeEp1) -> Ep1DeviceManager:
    mgr = Ep1DeviceManager.__new__(Ep1DeviceManager)
    mgr._devices = {device.identifier: device}  # type: ignore[attr-defined]
    mgr._fetched = True  # type: ignore[attr-defined]
    return mgr


def _lux_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                Ep1ReadingCompareCondition(
                    type="ep1_reading_compare",
                    comparison=Ep1ReadingComparison.BELOW,
                    metric=Ep1ReadingMetric.ILLUMINANCE_LX,
                    threshold=80.0,
                    device=RuleConditionDeviceRefOut(
                        device_id=_MAC,
                        family_id=DeviceFamilyId.EP1,
                        display_name="Office EP1",
                    ),
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="lux-only",
        label="Lux only",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
    )


def _occupancy_rule() -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(
            all=[
                DevicesAnyInStateCondition(
                    type="devices_any_in_state",
                    state=DeviceConditionState.OCCUPIED,
                    devices=[
                        RuleConditionDeviceRefOut(
                            device_id=_MAC,
                            family_id=DeviceFamilyId.EP1,
                            display_name="Office EP1",
                        ),
                    ],
                ),
            ],
        ),
        cooldown_s=0,
        device_actions=[],
        enabled=True,
        id="occupancy-only",
        label="Occupancy only",
        min_location_accuracy_m=50,
        notification_emails=["ops@example.com"],
        notify_on_fire=True,
        triggers=[RuleTrigger.DEVICE_STATE],
    )


def _reading_kind_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    include_lux: bool,
    include_occupancy: bool,
) -> _ReadingKindFixture:
    bundle = tmp_path / "rules.json"
    db = tmp_path / "discovery.sqlite"
    rules: list[RuleOut] = []
    if include_occupancy:
        rules.append(_occupancy_rule())
    if include_lux:
        rules.append(_lux_rule())
    _write_bundle(bundle, rules)
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    device = _FakeEp1(
        _MAC,
        "Office EP1",
        illuminance_lx=20.0,
        occupancy_state=DeviceConditionState.OCCUPIED.value,
    )
    state = DeviceManagersState(
        kasa_mgr=MagicMock(spec=KasaDeviceManager),
        sonos_mgr=None,
        tailwind_mgr=None,
        androidtv_mgr=None,
        ep1_mgr=_ep1_mgr(device),
        vizio_mgr=None,
        cache_path=db,
        args=argparse.Namespace(),
    )
    evaluator = RuleEvaluator(
        cache_path=db,
        device_state_getter=lambda: state,
        now_fn=lambda: 1_700_000_000.0,
    )
    return {"evaluator": evaluator, "device": device}


def _write_bundle(path: Path, rules: list[RuleOut]) -> None:
    payload = {
        "version": 1,
        "device_id_resolution": "mac",
        "settings_location": {
            "lat": 41.194072,
            "lon": -73.8883254,
            "timezone": "America/New_York",
            "home_label": "Home",
        },
        "rules": [rule.model_dump(mode="json") for rule in rules],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
