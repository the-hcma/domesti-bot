"""Process-lifetime holder for the domesti-bot HTTP server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from app.device_enums import DeviceFamilyId
from app.device_state_change import DeviceStateChangeDetector
from app.device_state_watcher import (
    build_default_watchers,
    poll_interval_from_env,
    run_device_state_watchers,
)
from app.domesti_bot_cli import DeviceManagersState
from app.rule_evaluator import RuleEvaluator
from app.vacation_mode import handle_vacation_device_anomaly

_LOGGER = logging.getLogger(__name__)


def discovery_cache_path_from_cli_args(args: Any) -> Path | None:
    """Resolve the shared SQLite path (presence + device discovery cache)."""
    raw = getattr(args, "discovery_cache", None)
    if raw is None:
        return None
    return Path(str(raw)).expanduser().resolve()


class DomestiServerRuntime:
    """Mutable process singleton for server-wide services and discovery state."""

    cli_args: Any | None
    device_state: DeviceManagersState | None
    discovery_completed_at: float | None
    discovery_error: str | None
    discovery_started_at: float | None
    discovery_task: asyncio.Task[None] | None
    lifespan_generation: int
    rule_evaluator: RuleEvaluator | None
    shutdown_requested: asyncio.Event | None
    watcher_stop: asyncio.Event | None
    watcher_task: asyncio.Task[None] | None

    def __init__(self) -> None:
        self.reset()

    def bind_cli_args(self, args: Any) -> None:
        self.cli_args = args

    def begin_lifespan(self) -> None:
        """Create process-lifetime services when the ASGI lifespan starts."""
        self._cancel_background_tasks()
        self.lifespan_generation += 1
        self.device_state = None
        self.discovery_error = None
        self.discovery_started_at = time.monotonic()
        self.discovery_completed_at = None
        self.shutdown_requested = asyncio.Event()
        self.watcher_stop = asyncio.Event()
        self.watcher_task = None
        self.discovery_task = None
        if self.rule_evaluator is not None:
            self.rule_evaluator.request_shutdown()
        self.rule_evaluator = RuleEvaluator(
            cache_path=self.discovery_cache_path(),
            device_state_getter=lambda: self.device_state,
        )
        self.rule_evaluator.start_periodic_tick()

    async def close_rule_evaluator(self) -> None:
        evaluator = self.rule_evaluator
        if evaluator is None:
            return
        await evaluator.close()
        self.rule_evaluator = None

    def discovery_cache_path(self) -> Path | None:
        if self.cli_args is None:
            return None
        return discovery_cache_path_from_cli_args(self.cli_args)

    def is_shutdown_requested(self) -> bool:
        event = self.shutdown_requested
        return event is not None and event.is_set()

    def reset(self) -> None:
        if hasattr(self, "watcher_stop"):
            self._cancel_background_tasks()
        self.cli_args = None
        self.device_state = None
        self.discovery_completed_at = None
        self.discovery_error = None
        self.discovery_started_at = None
        self.discovery_task = None
        self.lifespan_generation = 0
        self.rule_evaluator = None
        self.shutdown_requested = None
        self._vacation_anomaly_tasks = set()
        self.watcher_stop = None
        self.watcher_task = None

    def build_device_state_change_detector(self) -> DeviceStateChangeDetector:
        return DeviceStateChangeDetector(self._on_device_bool_transition)

    def schedule_rule_location_evaluation(self, user_id: str) -> None:
        evaluator = self.rule_evaluator
        if evaluator is not None:
            evaluator.schedule_location_update(user_id)

    def schedule_rule_device_state_evaluation(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
    ) -> None:
        evaluator = self.rule_evaluator
        if evaluator is not None:
            evaluator.schedule_device_state_change(family_id, device_id)

    def schedule_vacation_anomaly_alert(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        previous: bool,
        current: bool | None,
    ) -> None:
        """Queue vacation anomaly handling off the device-state poll path."""
        cache_path = self.discovery_cache_path()
        if cache_path is None:
            return

        def _run() -> None:
            handle_vacation_device_anomaly(
                cache_path,
                family_id=family_id,
                device_id=device_id,
                previous=previous,
                current=current,
            )

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _run()
            return
        task = loop.create_task(asyncio.to_thread(_run), name="vacation-anomaly")
        self._vacation_anomaly_tasks.add(task)
        task.add_done_callback(self._vacation_anomaly_tasks.discard)

    def signal_shutdown(self) -> None:
        """Tell every background loop to stop starting new work."""
        if self.shutdown_requested is not None:
            self.shutdown_requested.set()
        if self.watcher_stop is not None:
            self.watcher_stop.set()
        evaluator = self.rule_evaluator
        if evaluator is not None:
            evaluator.request_shutdown()

    async def restart_device_state_watchers(self) -> None:
        """Rebuild background polling after a hot-reloaded device manager."""
        if self.is_shutdown_requested():
            return
        state = self.device_state
        if state is None:
            return
        if self.watcher_stop is not None:
            self.watcher_stop.set()
        watcher_task = self.watcher_task
        if watcher_task is not None and not watcher_task.done():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher_task
        try:
            poll_interval_s = poll_interval_from_env()
        except ValueError as exc:
            _LOGGER.error(
                "[state-watcher] hot reload skipped — bad DOMESTI_STATE_POLL_INTERVAL_S: %s",
                exc,
            )
            return
        if self.is_shutdown_requested():
            return
        self.watcher_stop = asyncio.Event()
        watchers = build_default_watchers(
            state,
            change_detector=self.build_device_state_change_detector(),
            interval_s=poll_interval_s,
        )
        self.watcher_task = asyncio.create_task(
            run_device_state_watchers(watchers, stop=self.watcher_stop),
            name="device-state-watcher",
        )
        _LOGGER.info(
            "[state-watcher] restarted after hot reload; polling every %.1fs across %d backend(s)",
            poll_interval_s,
            len(watchers),
        )

    @contextlib.contextmanager
    def temporary_device_state(
        self,
        state: DeviceManagersState,
    ) -> Iterator[DeviceManagersState]:
        """Install ``state`` (and its CLI args) for the block, then restore priors.

        Prefer this over hand-rolled ``try`` / ``finally`` in tests and one-shot
        hot-reload helpers that need a live ``device_state`` snapshot.
        """
        previous_args = self.cli_args
        previous_state = self.device_state
        self.bind_cli_args(state.args)
        self.device_state = state
        try:
            yield state
        finally:
            self.device_state = previous_state
            self.cli_args = previous_args

    def _cancel_background_tasks(self) -> None:
        if self.watcher_stop is not None:
            self.watcher_stop.set()
        if self.watcher_task is not None and not self.watcher_task.done():
            self.watcher_task.cancel()
        if self.discovery_task is not None and not self.discovery_task.done():
            self.discovery_task.cancel()
        for task in list(self._vacation_anomaly_tasks):
            if not task.done():
                task.cancel()
        self._vacation_anomaly_tasks.clear()

    def _on_device_bool_transition(
        self,
        family_id: DeviceFamilyId,
        device_id: str,
        previous: bool,
        current: bool | None,
    ) -> None:
        self.schedule_rule_device_state_evaluation(family_id, device_id)
        self.schedule_vacation_anomaly_alert(
            family_id,
            device_id,
            previous,
            current,
        )


runtime = DomestiServerRuntime()
