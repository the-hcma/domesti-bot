"""Tests for RuleEvaluator.schedule_location_update thread safety."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from app.rule_evaluator import RuleEvaluator


@pytest.mark.asyncio
async def test_schedule_location_update_after_close_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = tmp_path / "cache.sqlite"
    evaluator = RuleEvaluator(cache_path=db, device_state_getter=lambda: None)
    evaluator.start_periodic_tick()
    event_loop = evaluator._event_loop
    assert event_loop is not None
    await evaluator.close()

    def raise_closed(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Event loop is closed")

    thread_errors: list[BaseException] = []

    def worker() -> None:
        try:
            with (
                patch.object(event_loop, "call_soon_threadsafe", side_effect=raise_closed),
                caplog.at_level(logging.WARNING, logger="app.rule_evaluator"),
            ):
                evaluator.schedule_location_update("henrique")
        except BaseException as exc:
            thread_errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(timeout=5.0)
    assert not thread_errors
    assert "event loop is closed" in caplog.text


@pytest.mark.asyncio
async def test_schedule_location_update_from_worker_thread(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    evaluator = RuleEvaluator(cache_path=db, device_state_getter=lambda: None)
    evaluator.start_periodic_tick()

    done = asyncio.Event()

    async def _process_stub(user_id: str) -> None:
        done.set()

    thread_errors: list[BaseException] = []

    def worker() -> None:
        try:
            evaluator.schedule_location_update("henrique")
        except BaseException as exc:
            thread_errors.append(exc)

    with patch.object(evaluator, "_process_location_update", side_effect=_process_stub):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread_errors
        assert not thread.is_alive()
        await asyncio.wait_for(done.wait(), timeout=5.0)

    await evaluator.close()


@pytest.mark.asyncio
async def test_schedule_location_update_on_event_loop_thread(tmp_path: Path) -> None:
    db = tmp_path / "cache.sqlite"
    evaluator = RuleEvaluator(cache_path=db, device_state_getter=lambda: None)
    evaluator.start_periodic_tick()

    done = asyncio.Event()

    async def _process_stub(user_id: str) -> None:
        done.set()

    with patch.object(evaluator, "_process_location_update", side_effect=_process_stub):
        evaluator.schedule_location_update("henrique")
        await asyncio.wait_for(done.wait(), timeout=5.0)

    await evaluator.close()


def test_schedule_location_update_without_event_loop_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    db = tmp_path / "cache.sqlite"
    evaluator = RuleEvaluator(cache_path=db, device_state_getter=lambda: None)

    with caplog.at_level(logging.WARNING, logger="app.rule_evaluator"):
        evaluator.schedule_location_update("henrique")

    assert "no event loop registered yet" in caplog.text
