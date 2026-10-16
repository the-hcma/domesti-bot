"""Faster polling for the devices a turn-on rule watches while its time window is open."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

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
from app.device_enums import DeviceConditionState, DeviceFamilyId, RuleTrigger
from app.device_state_watcher import KasaPollingWatcher
from app.fast_poll_targets import (
    FAST_POLL_INTERVAL_S,
    FastPollTargetsReader,
    fast_poll_device_key,
    fast_poll_targets_at,
)
from app.kasa_device_manager import KasaDeviceManager

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


def test_reader_follows_the_rules_and_keeps_the_last_answer_when_they_fail_to_load() -> None:
    reader = FastPollTargetsReader(now_fn=lambda: _at(7, 0))
    settings = MagicMock(timezone="America/New_York")
    with (
        patch("app.fast_poll_targets.list_automation_rules", return_value=[_window_rule()]),
        patch("app.fast_poll_targets.load_settings_location", return_value=settings),
    ):
        assert reader() == _STAIRS
    with (
        patch("app.fast_poll_targets.list_automation_rules", side_effect=ValueError("bad rules file")),
        patch("app.fast_poll_targets.load_settings_location", return_value=settings),
    ):
        assert reader() == _STAIRS
    with (
        patch("app.fast_poll_targets.list_automation_rules", return_value=[]),
        patch("app.fast_poll_targets.load_settings_location", return_value=settings),
    ):
        assert reader() == frozenset()


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
