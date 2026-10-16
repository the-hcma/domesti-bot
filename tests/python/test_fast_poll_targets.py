"""Faster polling for the devices a turn-on rule watches while its time window is open."""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import app.api.app as api_app_module
from app.api.schemas import (
    AnyConditionsCondition,
    DevicesAnyInStateCondition,
    DevicesAnyTransitionedToStateCondition,
    LocalTimeWindowCondition,
    RuleConditionDeviceRefOut,
    RuleConditionOut,
    RuleConditionsOut,
    RuleOut,
)
from app.automation_rules_loader import load_automation_rules_bundle
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.device_state_watcher import KasaPollingWatcher, build_default_watchers
from app.domesti_bot_cli import DeviceManagersState
from app.fast_poll_targets import (
    FAST_POLL_INTERVAL_S,
    FastPollTargetsReader,
    fast_poll_device_key,
    fast_poll_targets_at,
)
from app.kasa_device_manager import KasaDeviceManager
from app.server_runtime import DomestiServerRuntime

_STAIRS_MAC = "5c:e9:31:60:c5:01"
_OTHER_MAC = "5c:e9:31:60:c5:02"
_TZ = ZoneInfo("America/New_York")


def _at(hour: int, minute: int) -> datetime:
    return datetime(2026, 10, 15, hour, minute, tzinfo=_TZ)


def _morning_or_evening() -> AnyConditionsCondition:
    return AnyConditionsCondition(
        type="any",
        conditions=[
            LocalTimeWindowCondition(type="local_time_window", start_hhmm="06:30", end_hhmm="09:30"),
            LocalTimeWindowCondition(type="local_time_window", start_hhmm="19:00", end_hhmm="22:00"),
        ],
    )


def _transition(device_id: str = _STAIRS_MAC) -> DevicesAnyTransitionedToStateCondition:
    return DevicesAnyTransitionedToStateCondition(
        type="devices_any_transitioned_to_state",
        devices=[RuleConditionDeviceRefOut(device_id=device_id, family_id=DeviceFamilyId.KASA)],
        state=DeviceConditionState.ON,
    )


def _rule(
    conditions: list[RuleConditionOut],
    *,
    enabled: bool = True,
    rule_id: str = "stairs-companions",
    triggers: list[RuleTrigger] | None = None,
) -> RuleOut:
    return RuleOut(
        conditions=RuleConditionsOut(all=conditions),
        cooldown_s=0,
        device_actions=[],
        enabled=enabled,
        id=rule_id,
        label=rule_id,
        min_location_accuracy_m=50,
        notification_emails=[],
        notify_on_fire=False,
        triggers=triggers if triggers is not None else [RuleTrigger.DEVICE_STATE],
    )


def _window_rule(
    start: str = "06:30",
    end: str = "09:30",
    *,
    enabled: bool = True,
    rule_id: str = "stairs-companions",
) -> RuleOut:
    window = LocalTimeWindowCondition(type="local_time_window", start_hhmm=start, end_hhmm=end)
    return _rule([_transition(), window], enabled=enabled, rule_id=rule_id)


_STAIRS = frozenset({(DeviceFamilyId.KASA, _STAIRS_MAC)})


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(6, 29, False), (6, 30, True), (9, 29, True), (9, 30, False), (12, 0, False)],
)
def test_a_single_window_gates_the_fast_polling_with_the_end_excluded(hour: int, minute: int, expected: bool) -> None:
    targets = fast_poll_targets_at([_window_rule()], now=_at(hour, minute))
    assert targets == (_STAIRS if expected else frozenset())


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(6, 29, False), (6, 30, True), (9, 30, False), (18, 59, False), (19, 0, True), (21, 59, True), (22, 0, False)],
)
def test_any_of_two_windows_polls_fast_inside_either_one(hour: int, minute: int, expected: bool) -> None:
    rule = _rule([_transition(), _morning_or_evening()])
    assert fast_poll_targets_at([rule], now=_at(hour, minute)) == (_STAIRS if expected else frozenset())


@pytest.mark.parametrize(("hour", "minute", "expected"), [(21, 59, False), (22, 0, True), (1, 59, True), (2, 0, False)])
def test_a_window_crossing_midnight_works(hour: int, minute: int, expected: bool) -> None:
    rule = _window_rule("22:00", "02:00")
    assert fast_poll_targets_at([rule], now=_at(hour, minute)) == (_STAIRS if expected else frozenset())


def test_a_disabled_rule_polls_nothing_fast() -> None:
    assert fast_poll_targets_at([_window_rule(enabled=False)], now=_at(7, 0)) == frozenset()


def test_a_rule_that_is_not_a_turn_on_rule_polls_nothing_fast() -> None:
    level_rule = _rule(
        [
            DevicesAnyInStateCondition(
                type="devices_any_in_state",
                devices=[RuleConditionDeviceRefOut(device_id=_STAIRS_MAC, family_id=DeviceFamilyId.KASA)],
                state=DeviceConditionState.ON,
            ),
        ],
    )
    assert fast_poll_targets_at([level_rule], now=_at(7, 0)) == frozenset()


def test_a_turn_on_rule_without_a_window_polls_fast_all_day() -> None:
    assert fast_poll_targets_at([_rule([_transition()])], now=_at(3, 0)) == _STAIRS


def test_two_rules_sharing_a_device_name_it_once_and_a_closed_one_adds_nothing() -> None:
    morning = _window_rule("06:30", "09:30", rule_id="morning")
    evening = _window_rule("19:00", "22:00", rule_id="evening")
    other = _rule(
        [
            _transition(_OTHER_MAC),
            LocalTimeWindowCondition(type="local_time_window", start_hhmm="19:00", end_hhmm="22:00"),
        ],
        rule_id="other",
    )
    assert fast_poll_targets_at([morning, evening, other], now=_at(7, 0)) == _STAIRS
    assert fast_poll_targets_at([morning, evening, other], now=_at(20, 0)) == frozenset(
        {(DeviceFamilyId.KASA, _STAIRS_MAC), (DeviceFamilyId.KASA, _OTHER_MAC)},
    )


def test_device_references_are_compared_as_normalized_macs() -> None:
    assert fast_poll_device_key("5C-E9-31-60-C5-01") == _STAIRS_MAC
    assert fast_poll_device_key("  192.0.2.7 ") == "192.0.2.7"


def _write_bundle(path: Path, rules: list[RuleOut]) -> None:
    path.write_text(
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
                "rules": [rule.model_dump(mode="json") for rule in rules],
            },
        ),
        encoding="utf-8",
    )


def test_reader_uses_the_settings_timezone_not_utc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "rules.json"
    _write_bundle(bundle, [_window_rule()])
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    # 12:00 UTC is 08:00 in New York in October, inside 06:30-09:30; the UTC hour (12) is not.
    inside = FastPollTargetsReader(now_fn=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    outside = FastPollTargetsReader(now_fn=lambda: datetime(2026, 10, 15, 8, 0, tzinfo=UTC))
    assert inside() == _STAIRS
    assert outside() == frozenset()


def test_reader_parses_the_file_once_and_again_only_when_it_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "rules.json"
    _write_bundle(bundle, [_window_rule()])
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reader = FastPollTargetsReader(now_fn=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    with patch("app.fast_poll_targets.load_automation_rules_bundle", wraps=load_automation_rules_bundle) as loader:
        assert reader() == _STAIRS
        assert reader() == _STAIRS
        assert loader.call_count == 1
        _write_bundle(bundle, [_window_rule(enabled=False)])
        assert reader() == frozenset()
        assert loader.call_count == 2


def test_reader_keeps_the_last_answer_while_the_file_is_broken_and_recovers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    bundle = tmp_path / "rules.json"
    _write_bundle(bundle, [_window_rule()])
    monkeypatch.setenv("DOMESTI_AUTOMATION_RULES_FILE", str(bundle))
    reader = FastPollTargetsReader(now_fn=lambda: datetime(2026, 10, 15, 12, 0, tzinfo=UTC))
    assert reader() == _STAIRS
    bundle.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.INFO, logger="app.fast_poll_targets"):
        assert reader() == _STAIRS
        assert reader() == _STAIRS
        _write_bundle(bundle, [_window_rule(enabled=False)])
        assert reader() == frozenset()
    failures = [record for record in caplog.records if record.levelno == logging.ERROR]
    recoveries = [record for record in caplog.records if "resumed" in record.getMessage()]
    assert len(failures) == 1
    assert len(recoveries) == 1


class _FakeTime:
    """A clock the watcher's sleeper advances, so no test waits in real time."""

    def __init__(self, *, stop_after_sleeps: int) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._stop_after = stop_after_sleeps

    def clock(self) -> float:
        return self.now

    async def sleep(self, stop: asyncio.Event, delay_s: float) -> None:
        self.sleeps.append(delay_s)
        self.now += delay_s
        if len(self.sleeps) >= self._stop_after:
            stop.set()


def _kasa_mgr() -> KasaDeviceManager:
    switches = []
    for host, mac in (("192.0.2.7", _STAIRS_MAC), ("192.0.2.8", _OTHER_MAC)):
        device = MagicMock()
        device.identifier = host
        device.mac_address = mac
        device._kDevice.host = host
        switches.append(device)
    mgr = MagicMock(spec=KasaDeviceManager)
    mgr.switches = tuple(switches)
    mgr.is_on = AsyncMock(return_value=True)
    return cast(KasaDeviceManager, mgr)


def _polled(mgr: KasaDeviceManager) -> list[str]:
    return sorted(call.args[0] for call in cast(AsyncMock, mgr.is_on).await_args_list)


@pytest.mark.asyncio
async def test_the_watched_device_is_polled_every_five_seconds_and_the_rest_every_ten() -> None:
    mgr = _kasa_mgr()
    fake = _FakeTime(stop_after_sleeps=5)
    watcher = KasaPollingWatcher(
        mgr,
        clock=fake.clock,
        fast_poll_targets=lambda: _STAIRS,
        interval_s=10.0,
        sleep=fake.sleep,
    )

    await asyncio.wait_for(watcher.run(stop=asyncio.Event()), timeout=5.0)

    # Ticks at 0, 5, 10, 15 and 20 s: the watched light each time, the other one only on the full cycles.
    assert fake.sleeps == [FAST_POLL_INTERVAL_S] * 5
    assert _polled(mgr).count("192.0.2.7") == 5
    assert _polled(mgr).count("192.0.2.8") == 3


@pytest.mark.asyncio
async def test_without_a_watched_device_every_device_is_polled_only_on_the_global_interval() -> None:
    mgr = _kasa_mgr()
    fake = _FakeTime(stop_after_sleeps=3)
    watcher = KasaPollingWatcher(
        mgr,
        clock=fake.clock,
        fast_poll_targets=lambda: frozenset(),
        interval_s=10.0,
        sleep=fake.sleep,
    )

    await asyncio.wait_for(watcher.run(stop=asyncio.Event()), timeout=5.0)

    assert fake.sleeps == [10.0, 10.0, 10.0]
    assert _polled(mgr).count("192.0.2.7") == 3
    assert _polled(mgr).count("192.0.2.8") == 3


@pytest.mark.asyncio
async def test_the_fast_cadence_starts_and_stops_with_the_window() -> None:
    mgr = _kasa_mgr()
    fake = _FakeTime(stop_after_sleeps=4)
    answers = iter([frozenset(), _STAIRS, _STAIRS, frozenset()])
    watcher = KasaPollingWatcher(
        mgr,
        clock=fake.clock,
        fast_poll_targets=lambda: next(answers),
        interval_s=10.0,
        sleep=fake.sleep,
    )

    await asyncio.wait_for(watcher.run(stop=asyncio.Event()), timeout=5.0)

    # t=0 full (no window yet, so wait 10); t=10 full (window open, wait 5); t=15 fast; t=20 full (window closed).
    assert fake.sleeps == [10.0, 5.0, 5.0, 10.0]
    assert _polled(mgr).count("192.0.2.7") == 4
    assert _polled(mgr).count("192.0.2.8") == 3


@pytest.mark.asyncio
async def test_a_global_interval_below_the_fast_one_is_left_alone() -> None:
    mgr = _kasa_mgr()
    fake = _FakeTime(stop_after_sleeps=3)
    watcher = KasaPollingWatcher(
        mgr,
        clock=fake.clock,
        fast_poll_targets=lambda: _STAIRS,
        interval_s=2.0,
        sleep=fake.sleep,
    )

    await asyncio.wait_for(watcher.run(stop=asyncio.Event()), timeout=5.0)

    assert fake.sleeps == [2.0, 2.0, 2.0]
    assert _polled(mgr).count("192.0.2.8") == 3


@pytest.mark.asyncio
async def test_a_provider_that_raises_never_stops_the_kasa_watcher(caplog: pytest.LogCaptureFixture) -> None:
    mgr = _kasa_mgr()
    fake = _FakeTime(stop_after_sleeps=3)

    def _broken() -> frozenset[tuple[DeviceFamilyId, str]]:
        raise RuntimeError("provider blew up")

    watcher = KasaPollingWatcher(
        mgr,
        clock=fake.clock,
        fast_poll_targets=_broken,
        interval_s=10.0,
        sleep=fake.sleep,
    )

    with caplog.at_level(logging.ERROR, logger="app.device_state_watcher"):
        await asyncio.wait_for(watcher.run(stop=asyncio.Event()), timeout=5.0)

    assert fake.sleeps == [10.0, 10.0, 10.0]
    assert _polled(mgr).count("192.0.2.7") == 3
    assert len([record for record in caplog.records if "fast-poll targets unavailable" in record.getMessage()]) == 1


def _device_state(kasa_mgr: KasaDeviceManager) -> DeviceManagersState:
    return DeviceManagersState(
        androidtv_mgr=None,
        args=argparse.Namespace(),
        cache_path=Path("unused.sqlite"),
        ep1_mgr=None,
        kasa_mgr=kasa_mgr,
        sonos_mgr=None,
        tailwind_mgr=None,
        vizio_mgr=None,
    )


def test_build_default_watchers_hands_the_provider_to_the_kasa_watcher() -> None:
    def provider() -> frozenset[tuple[DeviceFamilyId, str]]:
        return _STAIRS

    watchers = build_default_watchers(_device_state(_kasa_mgr()), fast_poll_targets=provider, interval_s=10.0)

    kasa = next(watcher for watcher in watchers if isinstance(watcher, KasaPollingWatcher))
    assert kasa._fast_poll_targets is provider


def test_the_runtime_builds_a_provider_that_reads_the_rules_file() -> None:
    assert isinstance(DomestiServerRuntime().build_fast_poll_targets_provider(), FastPollTargetsReader)


@pytest.mark.asyncio
async def test_a_hot_reload_restarts_the_watchers_with_the_provider() -> None:
    runtime = DomestiServerRuntime()
    runtime.device_state = _device_state(_kasa_mgr())

    async def _idle(*_args: object, **_kwargs: object) -> None:
        return None

    with (
        patch("app.server_runtime.build_default_watchers", return_value=[]) as build,
        patch("app.server_runtime.run_device_state_watchers", new=_idle),
    ):
        await runtime.restart_device_state_watchers()
        assert runtime.watcher_task is not None
        await runtime.watcher_task

    assert isinstance(build.call_args.kwargs["fast_poll_targets"], FastPollTargetsReader)


def test_the_http_lifespan_passes_the_provider_when_it_builds_the_watchers() -> None:
    # The lifespan builds the watchers inside a closure that a unit test cannot reach without
    # a full discovery run, so pin the call itself: dropping the argument would silently
    # turn the feature off.
    source = inspect.getsource(api_app_module.create_app)
    call = re.search(r"build_default_watchers\((.*?)\n\s*\)\n", source, re.DOTALL)
    assert call is not None
    assert "fast_poll_targets=runtime.build_fast_poll_targets_provider()" in call.group(1)
