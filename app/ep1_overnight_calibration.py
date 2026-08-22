"""Overnight empty-room EP1 occupancy false-positive calibrator.

Assumes the target room is unoccupied during a local-time window (default
00:00–06:00). Observes the combined ``occupancy`` binary; any occupied reading
is treated as a false positive. On each false positive, lowers one mmWave knob
(or raises on-latency) via :func:`app.ep1_occupancy_tuning.apply_ep1_occupancy_tuning`,
logs the change, and continues until consecutive clear windows succeed or the
calibration window ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from datetime import time as dt_time
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aioesphomeapi.client import APIClient
from aioesphomeapi.core import APIConnectionError
from aioesphomeapi.model import BinarySensorInfo, BinarySensorState, EntityInfo, EntityState

from app.device_enums import Ep1OccupancyTuningKind
from app.ep1_calibration import Ep1SettingsTarget, list_ep1_settings_targets, resolve_ep1_settings_target
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import Ep1DeviceManager
from app.ep1_occupancy_tuning import (
    Ep1OccupancyTuningError,
    Ep1OccupancyTuningField,
    Ep1OccupancyTuningNotFoundError,
    Ep1OccupancyTuningSnapshot,
    apply_ep1_occupancy_tuning,
    read_ep1_occupancy_tuning,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_CLEAR_STREAK_REQUIRED = 3
DEFAULT_LOG_DIR = Path.home() / "scratch" / "domesti-bot" / "ep1-overnight-calibration"
DEFAULT_MAX_CONSECUTIVE_OBSERVE_FAILURES = 10
DEFAULT_OBSERVE_RETRY_COUNT = 3
DEFAULT_OBSERVE_RETRY_SLEEP_S = 5.0
DEFAULT_OBSERVE_S = 90.0
DEFAULT_SETTLE_S = 45.0
DEFAULT_WINDOW_END_HOUR = 6
DEFAULT_WINDOW_START_HOUR = 0
EP1_OVERNIGHT_CALIBRATION_EXHAUSTED = "All false-positive levers are already at their floor/ceiling for {device_id}"
EP1_OVERNIGHT_CALIBRATION_NO_OCCUPANCY = (
    "EP1 at {host}:{port} has no occupancy binary sensor (expected object_id aliases: {aliases})"
)
EP1_OVERNIGHT_CALIBRATION_OBSERVE_FAILURES_EXHAUSTED = (
    "EP1 occupancy observe failed {count} consecutive cycles for {device_id}; aborting"
)
EP1_OVERNIGHT_CALIBRATION_OUTSIDE_WINDOW = (
    "Local time {local_time} is outside the empty-room window "
    "[{start_hour:02d}:00, {end_hour:02d}:00); pass --force-window to override"
)
EP1_OVERNIGHT_CALIBRATION_TARGET_NOT_FOUND = "No EP1 device matched device_id={device_id!r}"
# Sub-second / tiny remaining windows often collect zero states; end cleanly instead.
MIN_USEFUL_OBSERVE_S = 5.0

_DECREASE_KINDS: frozenset[Ep1OccupancyTuningKind] = frozenset(
    {
        Ep1OccupancyTuningKind.MAX_DISTANCE,
        Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
        Ep1OccupancyTuningKind.TRIGGER_DISTANCE,
        Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
    }
)
_INCREASE_KINDS: frozenset[Ep1OccupancyTuningKind] = frozenset(
    {
        Ep1OccupancyTuningKind.MIN_DISTANCE,
        Ep1OccupancyTuningKind.ON_LATENCY,
    }
)
# Vendor empty-room checklist order: shrink range → desensitize → lengthen on-latency →
# raise min distance last (near-field clutter).
_LEVER_ORDER: tuple[Ep1OccupancyTuningKind, ...] = (
    Ep1OccupancyTuningKind.MAX_DISTANCE,
    Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
    Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
    Ep1OccupancyTuningKind.ON_LATENCY,
    Ep1OccupancyTuningKind.TRIGGER_DISTANCE,
    Ep1OccupancyTuningKind.MIN_DISTANCE,
)
_NUMBER_VALUE_ABS_TOL = 1e-6
_OCCUPANCY_ALIASES: tuple[str, ...] = ("occupancy",)
_installed_stop_signals: list[signal.Signals] = []


class Ep1OvernightCalibrationError(ValueError):
    """Operator-facing overnight calibrator failure."""


class Ep1OvernightCalibrationInterruptedError(Ep1OvernightCalibrationError):
    """Stop requested via ``stop_event`` / SIGTERM / SIGINT."""


class Ep1OvernightCalibrationNotFoundError(Ep1OvernightCalibrationError):
    """``device_id`` does not match a known EP1 target."""


class Ep1OvernightCalibrationOutsideWindowError(Ep1OvernightCalibrationError):
    """Local clock is outside the configured empty-room window."""


class KnobAdjustDirection(StrEnum):
    """Whether a lever move tightens (decrease) or loosens occupancy (increase)."""

    DECREASE = "decrease"
    INCREASE = "increase"


@dataclass(frozen=True, slots=True)
class KnobAdjustment:
    """One proposed or applied mmWave knob change."""

    direction: KnobAdjustDirection
    kind: Ep1OccupancyTuningKind
    new_value: float
    old_value: float
    step: float


@dataclass(frozen=True, slots=True)
class OccupancyObservation:
    """Result of watching the occupancy binary for a fixed duration."""

    duration_s: float
    false_positive: bool
    final_occupied: bool | None
    occupied_sample_count: int
    sample_count: int


@dataclass(frozen=True, slots=True)
class OvernightCalibrationCycleResult:
    """One observe → (optional) adjust cycle."""

    adjustment: KnobAdjustment | None
    applied: bool
    clear_streak: int
    dry_run: bool
    observation: OccupancyObservation
    knobs: Mapping[str, float | None]
    inconclusive: bool = False
    outside_window: bool = False


@dataclass(frozen=True, slots=True)
class OvernightCalibrationRunResult:
    """Summary after the overnight loop exits."""

    clear_streak: int
    cycles: int
    device_id: str
    display_label: str
    exhausted: bool
    false_positives: int
    success: bool
    window_ended: bool
    interrupted: bool = False


def default_calibration_log_path(*, device_id: str, now: datetime | None = None) -> Path:
    """JSONL path under ``$HOME/scratch/domesti-bot/ep1-overnight-calibration/``."""

    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    safe_id = device_id.replace(":", "").replace("/", "_")
    return DEFAULT_LOG_DIR / f"calibrate-ep1-{safe_id}-{stamp}.jsonl"


def in_empty_room_window(
    local_now: datetime,
    *,
    start_hour: int = DEFAULT_WINDOW_START_HOUR,
    end_hour: int = DEFAULT_WINDOW_END_HOUR,
) -> bool:
    """Return True when ``local_now`` is in ``[start_hour:00, end_hour:00)`` local time.

    Supports windows that wrap midnight (e.g. 22→6). ``start_hour == end_hour`` means
    the full day (always True).
    """

    _validate_hour(start_hour, name="start_hour")
    _validate_hour(end_hour, name="end_hour")
    if start_hour == end_hour:
        return True
    clock = local_now.timetz().replace(tzinfo=None)
    start = dt_time(hour=start_hour)
    end = dt_time(hour=end_hour)
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


def knob_values_for_log(snapshot: Ep1OccupancyTuningSnapshot) -> dict[str, float | None]:
    """Serialize available knob values for JSONL / CLI output."""

    out: dict[str, float | None] = {}
    for kind in Ep1OccupancyTuningKind:
        field = snapshot.knobs.get(kind)
        if field is None or not field.available:
            out[kind.value] = None
            continue
        out[kind.value] = field.value
    return out


async def observe_ep1_occupancy(
    *,
    host: str,
    port: int,
    duration_s: float,
    noise_psk: str | None = None,
    stop_event: asyncio.Event | None = None,
) -> OccupancyObservation:
    """Subscribe to the occupancy binary for ``duration_s`` and count occupied samples."""

    if not math.isfinite(duration_s) or duration_s <= 0:
        raise Ep1OvernightCalibrationError(f"Expected a positive duration_s, got {duration_s!r}")

    client = _ep1_api_client(host=host, port=port, noise_psk=noise_psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        occupancy = _occupancy_entity(entities)
        if occupancy is None:
            raise Ep1OvernightCalibrationError(
                EP1_OVERNIGHT_CALIBRATION_NO_OCCUPANCY.format(
                    host=host,
                    port=port,
                    aliases=", ".join(_OCCUPANCY_ALIASES),
                )
            )
        return await _watch_occupancy(
            client,
            key=int(occupancy.key),
            duration_s=duration_s,
            stop_event=stop_event,
        )
    except APIConnectionError as exc:
        raise Ep1OvernightCalibrationError(f"EP1 occupancy observe failed at {host}:{port}: {exc}") from exc
    finally:
        await _disconnect_client(client)


def install_overnight_calibration_stop_signals(stop_event: asyncio.Event) -> None:
    """Wire SIGTERM/SIGINT to ``stop_event`` on the running asyncio loop."""

    loop = asyncio.get_running_loop()

    def _on_stop() -> None:
        if stop_event.is_set():
            return
        _LOGGER.info("Stop signal received; exiting overnight calibration cleanly")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_stop)
        except (NotImplementedError, RuntimeError):
            # Fallback for environments without loop signal handlers.
            signal.signal(sig, lambda *_args: _on_stop())
        _installed_stop_signals.append(sig)


def propose_next_false_positive_adjustment(
    snapshot: Ep1OccupancyTuningSnapshot,
    *,
    attempt_index: int = 0,
) -> KnobAdjustment | None:
    """Pick the next lever to tighten after a false positive.

    Walks :data:`_LEVER_ORDER` starting at ``attempt_index % len(order)``, skipping
    unavailable knobs and those already at the floor (decrease) or ceiling (increase).
    """

    if not _LEVER_ORDER:
        return None
    start = attempt_index % len(_LEVER_ORDER)
    for offset in range(len(_LEVER_ORDER)):
        kind = _LEVER_ORDER[(start + offset) % len(_LEVER_ORDER)]
        field = snapshot.knobs.get(kind)
        if field is None or not field.available or field.value is None:
            continue
        proposed = _propose_for_field(field)
        if proposed is not None:
            return proposed
    return None


def remove_overnight_calibration_stop_signals() -> None:
    """Remove handlers installed by :func:`install_overnight_calibration_stop_signals`."""

    loop = asyncio.get_running_loop()
    while _installed_stop_signals:
        sig = _installed_stop_signals.pop()
        try:
            loop.remove_signal_handler(sig)
        except (NotImplementedError, RuntimeError):
            pass


async def run_overnight_ep1_calibration(
    *,
    device_id: str,
    cache_path: Path | None = None,
    clear_streak_required: int = DEFAULT_CLEAR_STREAK_REQUIRED,
    cli_noise_psk: str | None = None,
    dry_run: bool = False,
    ep1_mgr: Ep1DeviceManager | None = None,
    force_window: bool = False,
    log_path: Path | None = None,
    max_cycles: int | None = None,
    observe_s: float = DEFAULT_OBSERVE_S,
    settle_s: float = DEFAULT_SETTLE_S,
    stop_event: asyncio.Event | None = None,
    timezone_name: str | None = None,
    wait_for_window: bool = False,
    window_end_hour: int = DEFAULT_WINDOW_END_HOUR,
    window_start_hour: int = DEFAULT_WINDOW_START_HOUR,
) -> OvernightCalibrationRunResult:
    """Loop: observe → on false positive adjust one knob → settle → repeat until clear."""

    if clear_streak_required < 1:
        raise Ep1OvernightCalibrationError(f"Expected clear_streak_required >= 1, got {clear_streak_required}")
    if max_cycles is not None and max_cycles < 1:
        raise Ep1OvernightCalibrationError(f"Expected max_cycles >= 1, got {max_cycles}")

    target = resolve_ep1_settings_target(device_id, cache_path=cache_path, ep1_mgr=ep1_mgr)
    if target is None:
        raise Ep1OvernightCalibrationNotFoundError(
            EP1_OVERNIGHT_CALIBRATION_TARGET_NOT_FOUND.format(device_id=device_id)
        )

    tz = _resolve_calibration_timezone(timezone_name)
    local_now = datetime.now(tz=tz)
    while not force_window and not in_empty_room_window(
        local_now,
        start_hour=window_start_hour,
        end_hour=window_end_hour,
    ):
        if not wait_for_window:
            raise Ep1OvernightCalibrationOutsideWindowError(
                EP1_OVERNIGHT_CALIBRATION_OUTSIDE_WINDOW.format(
                    local_time=local_now.strftime("%H:%M:%S"),
                    start_hour=window_start_hour,
                    end_hour=window_end_hour,
                )
            )
        wait_s = seconds_until_empty_room_window(
            local_now,
            start_hour=window_start_hour,
            end_hour=window_end_hour,
        )
        # Chunk long waits so DST / clock changes are noticed before the window opens.
        sleep_s = min(wait_s, 60.0) if wait_s > 0 else 0.0
        _LOGGER.info(
            "Outside empty-room window; waiting %.0fs (next chunk %.0fs) until %02d:00 local",
            wait_s,
            sleep_s,
            window_start_hour,
        )
        if sleep_s > 0:
            if await _await_or_stop(stop_event, sleep_s):
                return OvernightCalibrationRunResult(
                    clear_streak=0,
                    cycles=0,
                    device_id=target.device_id,
                    display_label=target.display_label,
                    exhausted=False,
                    false_positives=0,
                    interrupted=True,
                    success=False,
                    window_ended=False,
                )
        local_now = datetime.now(tz=tz)

    resolved_log = log_path if log_path is not None else default_calibration_log_path(device_id=target.device_id)
    resolved_log.parent.mkdir(parents=True, exist_ok=True)
    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)

    _append_jsonl(
        resolved_log,
        {
            "event": "run_start",
            "device_id": target.device_id,
            "display_label": target.display_label,
            "dry_run": dry_run,
            "force_window": force_window,
            "observe_s": observe_s,
            "settle_s": settle_s,
            "window_start_hour": window_start_hour,
            "window_end_hour": window_end_hour,
            "clear_streak_required": clear_streak_required,
            "local_time": local_now.isoformat(timespec="seconds"),
        },
    )
    _LOGGER.info(
        "EP1 overnight calibration starting device_id=%s label=%s log=%s dry_run=%s",
        target.device_id,
        target.display_label,
        resolved_log,
        dry_run,
    )

    attempt_index = 0
    clear_streak = 0
    consecutive_observe_failures = 0
    cycles = 0
    exhausted = False
    false_positives = 0
    interrupted = False
    window_ended = False

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                interrupted = True
                _LOGGER.info("Stop requested; ending overnight calibration")
                break
            local_now = datetime.now(tz=tz)
            if not force_window and not in_empty_room_window(
                local_now,
                start_hour=window_start_hour,
                end_hour=window_end_hour,
            ):
                window_ended = True
                _LOGGER.info(
                    "Empty-room window ended at %s; stopping",
                    local_now.strftime("%H:%M:%S"),
                )
                break
            if max_cycles is not None and cycles >= max_cycles:
                break

            cycle_observe_s = observe_s
            if not force_window:
                remaining_s = seconds_until_empty_room_window_end(
                    local_now,
                    start_hour=window_start_hour,
                    end_hour=window_end_hour,
                )
                if remaining_s < MIN_USEFUL_OBSERVE_S:
                    window_ended = True
                    _LOGGER.info(
                        "Empty-room window ending (%.1fs left < %.1fs useful observe); stopping",
                        remaining_s,
                        MIN_USEFUL_OBSERVE_S,
                    )
                    break
                cycle_observe_s = min(observe_s, remaining_s)

            try:
                cycle = await _run_one_cycle(
                    target=target,
                    attempt_index=attempt_index,
                    cache_path=cache_path,
                    clear_streak=clear_streak,
                    dry_run=dry_run,
                    ep1_mgr=ep1_mgr,
                    force_window=force_window,
                    log_path=resolved_log,
                    noise_psk=psk,
                    observe_s=cycle_observe_s,
                    stop_event=stop_event,
                    timezone=tz,
                    window_end_hour=window_end_hour,
                    window_start_hour=window_start_hour,
                )
            except Ep1OvernightCalibrationInterruptedError:
                interrupted = True
                _LOGGER.info("Stop requested during observe; ending overnight calibration")
                break
            if cycle.outside_window:
                window_ended = True
                _LOGGER.info("Empty-room window ended mid-cycle; discarding observation")
                break
            if cycle.inconclusive:
                consecutive_observe_failures += 1
                _LOGGER.warning(
                    "Inconclusive observe cycle (%s consecutive); clear_streak unchanged at %s",
                    consecutive_observe_failures,
                    clear_streak,
                )
                if consecutive_observe_failures >= DEFAULT_MAX_CONSECUTIVE_OBSERVE_FAILURES:
                    raise Ep1OvernightCalibrationError(
                        EP1_OVERNIGHT_CALIBRATION_OBSERVE_FAILURES_EXHAUSTED.format(
                            count=consecutive_observe_failures,
                            device_id=target.device_id,
                        )
                    )
                continue
            consecutive_observe_failures = 0
            cycles += 1
            if cycle.observation.false_positive:
                false_positives += 1
                clear_streak = 0
                if cycle.adjustment is None:
                    exhausted = True
                    break
                # Only rotate levers after a confirmed write (or dry-run proposal).
                # Unconfirmed applies leave attempt_index alone so the same lever retries.
                if cycle.dry_run or cycle.applied:
                    attempt_index += 1
                if not cycle.dry_run and cycle.applied and settle_s > 0:
                    _LOGGER.info("Settling %.1fs after knob change", settle_s)
                    if await _await_or_stop(stop_event, settle_s):
                        interrupted = True
                        break
            else:
                clear_streak = cycle.clear_streak
                if clear_streak >= clear_streak_required:
                    break
    except Ep1OvernightCalibrationInterruptedError:
        interrupted = True
    except Exception as exc:
        _append_jsonl(
            resolved_log,
            {
                "event": "run_abort",
                "device_id": target.device_id,
                "cycles": cycles,
                "false_positives": false_positives,
                "clear_streak": clear_streak,
                "error": str(exc),
                "local_time": datetime.now(tz=tz).isoformat(timespec="seconds"),
            },
        )
        raise

    success = clear_streak >= clear_streak_required and not exhausted and not interrupted
    result = OvernightCalibrationRunResult(
        clear_streak=clear_streak,
        cycles=cycles,
        device_id=target.device_id,
        display_label=target.display_label,
        exhausted=exhausted,
        false_positives=false_positives,
        interrupted=interrupted,
        success=success,
        window_ended=window_ended,
    )
    _append_jsonl(
        resolved_log,
        {
            "event": "run_end",
            **asdict(result),
            "local_time": datetime.now(tz=tz).isoformat(timespec="seconds"),
        },
    )
    _LOGGER.info(
        "EP1 overnight calibration finished success=%s cycles=%s false_positives=%s "
        "clear_streak=%s exhausted=%s window_ended=%s interrupted=%s",
        result.success,
        result.cycles,
        result.false_positives,
        result.clear_streak,
        result.exhausted,
        result.window_ended,
        result.interrupted,
    )
    return result


def seconds_until_empty_room_window(
    local_now: datetime,
    *,
    start_hour: int = DEFAULT_WINDOW_START_HOUR,
    end_hour: int = DEFAULT_WINDOW_END_HOUR,
) -> float:
    """Seconds until ``local_now`` enters ``[start_hour:00, end_hour:00)``.

    Returns ``0.0`` when already inside the window. Supports windows that wrap
    midnight. ``start_hour == end_hour`` means always open (returns ``0.0``).
    """

    _validate_hour(start_hour, name="start_hour")
    _validate_hour(end_hour, name="end_hour")
    if in_empty_room_window(local_now, start_hour=start_hour, end_hour=end_hour):
        return 0.0
    start_today = local_now.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    if start_today <= local_now:
        start_today = start_today + timedelta(days=1)
    return max(0.0, (start_today - local_now).total_seconds())


def seconds_until_empty_room_window_end(
    local_now: datetime,
    *,
    start_hour: int = DEFAULT_WINDOW_START_HOUR,
    end_hour: int = DEFAULT_WINDOW_END_HOUR,
) -> float:
    """Seconds remaining in the open empty-room window, or ``0.0`` if closed.

    ``start_hour == end_hour`` (always open) returns a large sentinel so callers
    that cap observation duration do not truncate.
    """

    _validate_hour(start_hour, name="start_hour")
    _validate_hour(end_hour, name="end_hour")
    if start_hour == end_hour:
        return 24 * 3600.0
    if not in_empty_room_window(local_now, start_hour=start_hour, end_hour=end_hour):
        return 0.0
    end_at = local_now.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if end_at <= local_now:
        end_at = end_at + timedelta(days=1)
    return max(0.0, (end_at - local_now).total_seconds())


def select_ep1_calibration_target(
    device_id: str | None,
    *,
    cache_path: Path | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1SettingsTarget:
    """Resolve ``device_id`` or pick the sole discovered EP1."""

    if device_id:
        target = resolve_ep1_settings_target(device_id, cache_path=cache_path, ep1_mgr=ep1_mgr)
        if target is None:
            raise Ep1OvernightCalibrationNotFoundError(
                EP1_OVERNIGHT_CALIBRATION_TARGET_NOT_FOUND.format(device_id=device_id)
            )
        return target
    targets = list_ep1_settings_targets(cache_path=cache_path, ep1_mgr=ep1_mgr)
    if not targets:
        raise Ep1OvernightCalibrationNotFoundError("No EP1 devices discovered yet; run discovery or pass --device-id")
    if len(targets) > 1:
        listing = ", ".join(f"{row.display_label} ({row.device_id})" for row in targets)
        raise Ep1OvernightCalibrationError(f"Multiple EP1 devices found; pass --device-id. Known: {listing}")
    return targets[0]


def _adjustment_kwargs(adjustment: KnobAdjustment) -> dict[str, float]:
    return {adjustment.kind.value: adjustment.new_value}


def _append_jsonl(path: Path, payload: Mapping[str, object]) -> None:
    line = json.dumps(dict(payload), sort_keys=True, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


async def _await_or_stop(stop_event: asyncio.Event | None, delay_s: float) -> bool:
    """Sleep up to ``delay_s``. Return True if ``stop_event`` fired first."""

    if delay_s <= 0:
        return bool(stop_event is not None and stop_event.is_set())
    if stop_event is None:
        await asyncio.sleep(delay_s)
        return False
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay_s)
        return True
    except TimeoutError:
        return False


async def _disconnect_client(client: APIClient) -> None:
    try:
        await client.disconnect(force=True)
    except Exception:
        _LOGGER.debug("EP1 overnight calibration client disconnect failed", exc_info=True)


def _empty_occupancy_observation(duration_s: float) -> OccupancyObservation:
    """Placeholder when observe fails after retries (never treated as a clear)."""

    return OccupancyObservation(
        duration_s=duration_s,
        false_positive=False,
        final_occupied=None,
        occupied_sample_count=0,
        sample_count=0,
    )


def _ep1_api_client(*, host: str, port: int, noise_psk: str | None) -> APIClient:
    return APIClient(
        host,
        port,
        password=None,
        noise_psk=noise_psk,
        client_info="domesti-bot-ep1-overnight-calibration",
    )


def _entity_tokens(entity: EntityInfo) -> set[str]:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    return tokens


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _occupancy_entity(entities: Sequence[EntityInfo]) -> BinarySensorInfo | None:
    for entity in entities:
        if not isinstance(entity, BinarySensorInfo):
            continue
        if _entity_tokens(entity).intersection(_OCCUPANCY_ALIASES):
            return entity
    return None


def _propose_for_field(field: Ep1OccupancyTuningField) -> KnobAdjustment | None:
    if field.value is None or field.step is None or field.min_value is None or field.max_value is None:
        return None
    step = float(field.step)
    if step <= 0 or not math.isfinite(step):
        return None
    value = float(field.value)
    lo = float(field.min_value)
    hi = float(field.max_value)

    if field.kind in _DECREASE_KINDS:
        if value <= lo + _NUMBER_VALUE_ABS_TOL:
            return None
        new_value = max(
            lo,
            _quantize_toward(value - step, lo=lo, step=step, hi=hi, toward=KnobAdjustDirection.DECREASE),
        )
        if abs(new_value - value) <= _NUMBER_VALUE_ABS_TOL:
            return None
        return KnobAdjustment(
            direction=KnobAdjustDirection.DECREASE,
            kind=field.kind,
            new_value=new_value,
            old_value=value,
            step=step,
        )

    if field.kind in _INCREASE_KINDS:
        if value >= hi - _NUMBER_VALUE_ABS_TOL:
            return None
        new_value = min(
            hi,
            _quantize_toward(value + step, lo=lo, step=step, hi=hi, toward=KnobAdjustDirection.INCREASE),
        )
        if abs(new_value - value) <= _NUMBER_VALUE_ABS_TOL:
            return None
        return KnobAdjustment(
            direction=KnobAdjustDirection.INCREASE,
            kind=field.kind,
            new_value=new_value,
            old_value=value,
            step=step,
        )
    return None


def _quantize_toward(
    value: float,
    *,
    lo: float,
    step: float,
    hi: float,
    toward: KnobAdjustDirection,
) -> float:
    """Snap ``value`` onto the step grid, monotonic in ``toward`` for misaligned knobs."""

    raw = (value - lo) / step
    tol = max(_NUMBER_VALUE_ABS_TOL / step, 1e-9)
    if toward == KnobAdjustDirection.DECREASE:
        steps = math.floor(raw + tol)
    else:
        steps = math.ceil(raw - tol)
    quantized = lo + (steps * step)
    return min(hi, max(lo, quantized))


def _resolve_calibration_timezone(timezone_name: str | None) -> ZoneInfo:
    """Resolve an IANA zone for the empty-room window (explicit, TZ, or /etc/localtime)."""

    if timezone_name:
        try:
            return ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise Ep1OvernightCalibrationError(f"Expected a valid IANA timezone, got {timezone_name!r}") from exc

    env_tz = (os.environ.get("TZ") or "").strip()
    if env_tz:
        try:
            return ZoneInfo(env_tz)
        except (ZoneInfoNotFoundError, ValueError):
            pass

    system = _system_iana_timezone()
    if system is not None:
        return system

    raise Ep1OvernightCalibrationError("Could not resolve a local IANA timezone; pass --timezone Explicit/Name")


def _resolved_noise_psk(*, cli_noise_psk: str | None, cache_path: Path | None) -> str | None:
    psk, _source = resolve_ep1_noise_psk(cli_psk=cli_noise_psk, cache_path=cache_path)
    return (psk or "").strip() or None


async def _run_one_cycle(
    *,
    target: Ep1SettingsTarget,
    attempt_index: int,
    cache_path: Path | None,
    clear_streak: int,
    dry_run: bool,
    ep1_mgr: Ep1DeviceManager | None,
    force_window: bool,
    log_path: Path,
    noise_psk: str | None,
    observe_s: float,
    timezone: ZoneInfo,
    window_end_hour: int,
    window_start_hour: int,
    stop_event: asyncio.Event | None = None,
) -> OvernightCalibrationCycleResult:
    observation: OccupancyObservation | None = None
    observe_error: str | None = None
    for attempt in range(1, DEFAULT_OBSERVE_RETRY_COUNT + 1):
        if stop_event is not None and stop_event.is_set():
            raise Ep1OvernightCalibrationInterruptedError("Stop requested before occupancy observe")
        try:
            observation = await observe_ep1_occupancy(
                host=target.host,
                port=target.port,
                duration_s=observe_s,
                noise_psk=noise_psk,
                stop_event=stop_event,
            )
            break
        except Ep1OvernightCalibrationInterruptedError:
            raise
        except Ep1OvernightCalibrationError as exc:
            observe_error = str(exc)
            _LOGGER.warning(
                "EP1 occupancy observe failed (attempt %s/%s): %s",
                attempt,
                DEFAULT_OBSERVE_RETRY_COUNT,
                exc,
            )
            if attempt < DEFAULT_OBSERVE_RETRY_COUNT:
                if await _await_or_stop(stop_event, DEFAULT_OBSERVE_RETRY_SLEEP_S):
                    raise Ep1OvernightCalibrationInterruptedError(
                        "Stop requested during observe retry backoff"
                    ) from exc

    if observation is None:
        now = datetime.now(tz=timezone)
        placeholder = _empty_occupancy_observation(observe_s)
        _append_jsonl(
            log_path,
            {
                "event": "observe_inconclusive",
                "device_id": target.device_id,
                "error": observe_error,
                "observe_s": observe_s,
                "local_time": now.isoformat(timespec="seconds"),
            },
        )
        if not force_window and not in_empty_room_window(
            now,
            start_hour=window_start_hour,
            end_hour=window_end_hour,
        ):
            return OvernightCalibrationCycleResult(
                adjustment=None,
                applied=False,
                clear_streak=clear_streak,
                dry_run=dry_run,
                observation=placeholder,
                knobs={},
                outside_window=True,
            )
        return OvernightCalibrationCycleResult(
            adjustment=None,
            applied=False,
            clear_streak=clear_streak,
            dry_run=dry_run,
            observation=placeholder,
            knobs={},
            inconclusive=True,
        )
    now = datetime.now(tz=timezone)
    if not force_window and not in_empty_room_window(
        now,
        start_hour=window_start_hour,
        end_hour=window_end_hour,
    ):
        _append_jsonl(
            log_path,
            {
                "event": "window_ended_mid_cycle",
                "device_id": target.device_id,
                "observation": asdict(observation),
                "local_time": now.isoformat(timespec="seconds"),
            },
        )
        return OvernightCalibrationCycleResult(
            adjustment=None,
            applied=False,
            clear_streak=clear_streak,
            dry_run=dry_run,
            observation=observation,
            knobs={},
            outside_window=True,
        )
    try:
        snapshot = await read_ep1_occupancy_tuning(
            device_id=target.device_id,
            cache_path=cache_path,
            cli_noise_psk=noise_psk,
            ep1_mgr=ep1_mgr,
        )
    except Ep1OccupancyTuningNotFoundError as exc:
        raise Ep1OvernightCalibrationNotFoundError(str(exc)) from exc
    except Ep1OccupancyTuningError as exc:
        raise Ep1OvernightCalibrationError(str(exc)) from exc
    knobs = knob_values_for_log(snapshot)
    adjustment: KnobAdjustment | None = None
    applied = False
    next_clear_streak = clear_streak

    if observation.false_positive:
        adjustment = propose_next_false_positive_adjustment(snapshot, attempt_index=attempt_index)
        if adjustment is None:
            _append_jsonl(
                log_path,
                {
                    "event": "false_positive_exhausted",
                    "device_id": target.device_id,
                    "observation": asdict(observation),
                    "knobs": knobs,
                },
            )
            _LOGGER.warning(EP1_OVERNIGHT_CALIBRATION_EXHAUSTED.format(device_id=target.device_id))
        else:
            _LOGGER.warning(
                "False positive: occupied_samples=%s/%s; propose %s %s → %s (%s)",
                observation.occupied_sample_count,
                observation.sample_count,
                adjustment.kind.value,
                adjustment.old_value,
                adjustment.new_value,
                adjustment.direction.value,
            )
            _append_jsonl(
                log_path,
                {
                    "event": "false_positive",
                    "device_id": target.device_id,
                    "observation": asdict(observation),
                    "proposed": asdict(adjustment),
                    "knobs_before": knobs,
                    "dry_run": dry_run,
                },
            )
            if not dry_run:
                try:
                    updated = await apply_ep1_occupancy_tuning(
                        device_id=target.device_id,
                        cache_path=cache_path,
                        cli_noise_psk=noise_psk,
                        ep1_mgr=ep1_mgr,
                        **_adjustment_kwargs(adjustment),
                    )
                except Ep1OccupancyTuningNotFoundError as exc:
                    raise Ep1OvernightCalibrationNotFoundError(str(exc)) from exc
                except Ep1OccupancyTuningError as exc:
                    raise Ep1OvernightCalibrationError(str(exc)) from exc
                applied = bool(updated.knobs_confirmed)
                if not updated.knobs_confirmed:
                    _LOGGER.warning(
                        "Device did not confirm %s=%s; re-reading next cycle",
                        adjustment.kind.value,
                        adjustment.new_value,
                    )
                knobs = knob_values_for_log(updated)
                _append_jsonl(
                    log_path,
                    {
                        "event": "adjustment_applied",
                        "device_id": target.device_id,
                        "adjustment": asdict(adjustment),
                        "knobs_after": knobs,
                        "distance_applied": updated.distance_applied,
                        "sensitivity_applied": updated.sensitivity_applied,
                        "knobs_confirmed": updated.knobs_confirmed,
                    },
                )
                _LOGGER.info(
                    "Applied %s=%s (was %s); distance_applied=%s sensitivity_applied=%s",
                    adjustment.kind.value,
                    adjustment.new_value,
                    adjustment.old_value,
                    updated.distance_applied,
                    updated.sensitivity_applied,
                )
            else:
                _LOGGER.info(
                    "Dry-run: would set %s=%s (was %s)",
                    adjustment.kind.value,
                    adjustment.new_value,
                    adjustment.old_value,
                )
        next_clear_streak = 0
    else:
        next_clear_streak = clear_streak + 1
        _LOGGER.info(
            "Clear observation (samples=%s occupied=%s); clear_streak=%s",
            observation.sample_count,
            observation.occupied_sample_count,
            next_clear_streak,
        )
        _append_jsonl(
            log_path,
            {
                "event": "clear",
                "device_id": target.device_id,
                "observation": asdict(observation),
                "clear_streak": next_clear_streak,
                "knobs": knobs,
            },
        )

    return OvernightCalibrationCycleResult(
        adjustment=adjustment,
        applied=applied,
        clear_streak=next_clear_streak,
        dry_run=dry_run,
        observation=observation,
        knobs=knobs,
    )


def _system_iana_timezone() -> ZoneInfo | None:
    """Best-effort IANA zone from ``/etc/localtime`` (common Linux layout)."""

    path = Path("/etc/localtime")
    try:
        resolved = path.resolve()
    except OSError:
        return None
    parts = resolved.parts
    if "zoneinfo" not in parts:
        return None
    idx = parts.index("zoneinfo")
    name = "/".join(parts[idx + 1 :])
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return None


def _validate_hour(hour: int, *, name: str) -> None:
    if hour < 0 or hour > 23:
        raise Ep1OvernightCalibrationError(f"Expected {name} in 0..23, got {hour}")


async def _watch_occupancy(
    client: APIClient,
    *,
    key: int,
    duration_s: float,
    stop_event: asyncio.Event | None = None,
) -> OccupancyObservation:
    samples: list[bool] = []
    latest: bool | None = None

    def _on_state(state: EntityState) -> None:
        nonlocal latest
        if int(state.key) != key:
            return
        if not isinstance(state, BinarySensorState):
            return
        if getattr(state, "missing_state", False):
            return
        occupied = bool(state.state)
        latest = occupied
        samples.append(occupied)

    client.subscribe_states(_on_state)
    if await _await_or_stop(stop_event, duration_s):
        raise Ep1OvernightCalibrationInterruptedError("Occupancy observe interrupted by stop signal")
    occupied_count = sum(1 for sample in samples if sample)
    if not samples:
        raise Ep1OvernightCalibrationError(
            f"EP1 occupancy observe collected no states in {duration_s:g}s "
            f"(key={key}); is the occupancy binary available?"
        )
    return OccupancyObservation(
        duration_s=duration_s,
        false_positive=occupied_count > 0 or latest is True,
        final_occupied=latest,
        occupied_sample_count=occupied_count,
        sample_count=len(samples),
    )
