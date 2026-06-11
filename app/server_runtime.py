"""Process-lifetime holder for the domesti-bot HTTP server."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from app.domesti_bot_cli import DeviceManagersState
from app.rule_evaluator import RuleEvaluator


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
        self.watcher_stop = None
        self.watcher_task = None

    def schedule_rule_location_evaluation(self, user_id: str) -> None:
        evaluator = self.rule_evaluator
        if evaluator is not None:
            evaluator.schedule_location_update(user_id)

    def _cancel_background_tasks(self) -> None:
        if self.watcher_stop is not None:
            self.watcher_stop.set()
        if self.watcher_task is not None and not self.watcher_task.done():
            self.watcher_task.cancel()
        if self.discovery_task is not None and not self.discovery_task.done():
            self.discovery_task.cancel()


runtime = DomestiServerRuntime()
