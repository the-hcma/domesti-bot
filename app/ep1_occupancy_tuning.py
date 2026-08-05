"""EP1 mmWave occupancy tuning via ESPHome ``number`` / ``button`` entities.

Stock Everything Presence One (SEN0609) firmware exposes distance, sensitivity,
and latency numbers. Distance min/max and trigger/sustain sensitivity require
pressing ``Set Distance`` / ``Set Sensitivity`` after the number values change;
on/off latency and trigger distance apply via the number's ``set_action``.

Settings reads and writes use an ephemeral :class:`~aioesphomeapi.client.APIClient`
so the live subscription watcher is not disrupted.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from aioesphomeapi.client import APIClient
from aioesphomeapi.core import APIConnectionError
from aioesphomeapi.model import ButtonInfo, EntityInfo, EntityState, NumberInfo, NumberState

from app.device_enums import Ep1OccupancyApplyButton, Ep1OccupancyTuningKind
from app.ep1_calibration import Ep1SettingsTarget, resolve_ep1_settings_target
from app.ep1_credentials import resolve_ep1_noise_psk
from app.ep1_device_manager import Ep1DeviceManager

_LOGGER = logging.getLogger(__name__)

EP1_OCCUPANCY_TUNING_BUTTON_UNAVAILABLE = (
    "EP1 at {host}:{port} has no {button} button entity "
    "(expected object_id aliases {aliases}); required after changing {kinds}"
)
EP1_OCCUPANCY_TUNING_DEVICE_NOT_FOUND = "No EP1 device matched device_id={device_id!r}"
EP1_OCCUPANCY_TUNING_KNOB_UNAVAILABLE = (
    "EP1 at {host}:{port} has no {kind} number entity (expected object_id aliases {aliases})"
)
EP1_OCCUPANCY_TUNING_STATE_INCOMPLETE = (
    "Incomplete EP1 occupancy tuning states at {host}:{port} (got {got} of {expected} number keys)"
)
EP1_OCCUPANCY_TUNING_STATE_TIMEOUT = "Timed out waiting for EP1 occupancy tuning states at {host}:{port}"

_BUTTON_ALIASES: dict[Ep1OccupancyApplyButton, tuple[str, ...]] = {
    Ep1OccupancyApplyButton.SET_DISTANCE: ("set_distance",),
    Ep1OccupancyApplyButton.SET_SENSITIVITY: ("set_sensitivity",),
}
_DISTANCE_KINDS: frozenset[Ep1OccupancyTuningKind] = frozenset(
    {
        Ep1OccupancyTuningKind.MAX_DISTANCE,
        Ep1OccupancyTuningKind.MIN_DISTANCE,
    }
)
_KNOB_ENTITY_ALIASES: dict[Ep1OccupancyTuningKind, tuple[str, ...]] = {
    Ep1OccupancyTuningKind.MAX_DISTANCE: (
        "mmwave_max_distance",
        "mmwave_distance_max",
        "mmwave_distance",
    ),
    Ep1OccupancyTuningKind.MIN_DISTANCE: (
        "mmwave_minimum_distance",
        "mmwave_distance_min",
        "mmwave_min_distance",
    ),
    Ep1OccupancyTuningKind.OFF_LATENCY: ("mmwave_off_latency",),
    Ep1OccupancyTuningKind.ON_LATENCY: ("mmwave_on_latency",),
    Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY: (
        "mmwave_sustain_sensitivity",
        "mmwave_sensitivity",
    ),
    Ep1OccupancyTuningKind.TRIGGER_DISTANCE: ("mmwave_trigger_distance",),
    Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY: ("mmwave_trigger_sensitivity",),
}
_NUMBER_VALUE_ABS_TOL = 1e-6
_POST_BUTTON_SETTLE_S = 4.0
_POST_WRITE_TIMEOUT_S = 12.0
_SEN0395_DISTANCE_ALIASES: frozenset[str] = frozenset({"mmwave_distance"})
_SEN0395_SENSITIVITY_ALIASES: frozenset[str] = frozenset({"mmwave_sensitivity"})
_SEN0609_DISTANCE_ALIASES: frozenset[str] = frozenset(
    {
        "mmwave_distance_max",
        "mmwave_distance_min",
        "mmwave_max_distance",
        "mmwave_min_distance",
        "mmwave_minimum_distance",
    }
)
_SEN0609_SENSITIVITY_ALIASES: frozenset[str] = frozenset(
    {
        "mmwave_sustain_sensitivity",
        "mmwave_trigger_sensitivity",
    }
)
_SENSITIVITY_KINDS: frozenset[Ep1OccupancyTuningKind] = frozenset(
    {
        Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY,
        Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY,
    }
)
_STATE_COLLECT_TIMEOUT_S = 8.0


class Ep1OccupancyTuningError(ValueError):
    """Operator-facing occupancy tuning failure (maps to HTTP 4xx/502)."""


class Ep1OccupancyTuningNotFoundError(Ep1OccupancyTuningError):
    """``device_id`` does not match a known EP1 target."""


class Ep1OccupancyTuningValidationError(Ep1OccupancyTuningError):
    """Knob out of range, required number missing, or required apply button missing."""


@dataclass(frozen=True, slots=True)
class Ep1OccupancyTuningField:
    """One ESPHome mmWave occupancy number."""

    available: bool
    kind: Ep1OccupancyTuningKind
    max_value: float | None
    min_value: float | None
    step: float | None
    unit: str | None
    value: float | None


@dataclass(frozen=True, slots=True)
class Ep1OccupancyTuningSnapshot:
    """Occupancy tuning knobs for one EP1 target."""

    device_id: str
    display_label: str
    display_name: str | None
    host: str
    knobs: Mapping[Ep1OccupancyTuningKind, Ep1OccupancyTuningField]
    port: int
    distance_applied: bool = False
    knobs_confirmed: bool = True
    sensitivity_applied: bool = False


async def apply_ep1_occupancy_tuning(
    *,
    device_id: str,
    max_distance: float | None = None,
    min_distance: float | None = None,
    off_latency: float | None = None,
    on_latency: float | None = None,
    sustain_sensitivity: float | None = None,
    trigger_distance: float | None = None,
    trigger_sensitivity: float | None = None,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1OccupancyTuningSnapshot:
    """Write one or more mmWave knobs on ``device_id``, then return a fresh snapshot."""

    updates = _requested_knob_updates(
        max_distance=max_distance,
        min_distance=min_distance,
        off_latency=off_latency,
        on_latency=on_latency,
        sustain_sensitivity=sustain_sensitivity,
        trigger_distance=trigger_distance,
        trigger_sensitivity=trigger_sensitivity,
    )
    if not updates:
        return await read_ep1_occupancy_tuning(
            device_id=device_id,
            cache_path=cache_path,
            cli_noise_psk=cli_noise_psk,
            ep1_mgr=ep1_mgr,
        )

    target = resolve_ep1_settings_target(
        device_id,
        cache_path=cache_path,
        ep1_mgr=ep1_mgr,
    )
    if target is None:
        raise Ep1OccupancyTuningNotFoundError(EP1_OCCUPANCY_TUNING_DEVICE_NOT_FOUND.format(device_id=device_id))

    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        kind_to_number = _number_entities_by_kind(entities)
        buttons = _button_entities_by_role(entities)
        pending_writes: list[tuple[NumberInfo, float]] = []
        for kind, value in updates.items():
            number = kind_to_number.get(kind)
            if number is None:
                raise Ep1OccupancyTuningValidationError(
                    EP1_OCCUPANCY_TUNING_KNOB_UNAVAILABLE.format(
                        host=target.host,
                        port=target.port,
                        kind=kind.value,
                        aliases=_KNOB_ENTITY_ALIASES[kind],
                    )
                )
            _validate_knob_in_range(kind=kind, number=number, value=value)
            pending_writes.append((number, float(value)))

        need_distance = bool(set(updates) & _DISTANCE_KINDS)
        need_sensitivity = bool(set(updates) & _SENSITIVITY_KINDS)
        if (
            need_distance
            and _distance_apply_button_required(entities)
            and (Ep1OccupancyApplyButton.SET_DISTANCE not in buttons)
        ):
            raise Ep1OccupancyTuningValidationError(
                EP1_OCCUPANCY_TUNING_BUTTON_UNAVAILABLE.format(
                    host=target.host,
                    port=target.port,
                    button=Ep1OccupancyApplyButton.SET_DISTANCE.value,
                    aliases=_BUTTON_ALIASES[Ep1OccupancyApplyButton.SET_DISTANCE],
                    kinds=sorted(k.value for k in set(updates) & _DISTANCE_KINDS),
                )
            )
        if (
            need_sensitivity
            and _sensitivity_apply_button_required(entities)
            and (Ep1OccupancyApplyButton.SET_SENSITIVITY not in buttons)
        ):
            raise Ep1OccupancyTuningValidationError(
                EP1_OCCUPANCY_TUNING_BUTTON_UNAVAILABLE.format(
                    host=target.host,
                    port=target.port,
                    button=Ep1OccupancyApplyButton.SET_SENSITIVITY.value,
                    aliases=_BUTTON_ALIASES[Ep1OccupancyApplyButton.SET_SENSITIVITY],
                    kinds=sorted(k.value for k in set(updates) & _SENSITIVITY_KINDS),
                )
            )

        for number, value in pending_writes:
            # aioesphomeapi ``number_command`` is sync (sends NumberCommandRequest).
            client.number_command(int(number.key), value)

        distance_applied = False
        sensitivity_applied = False
        if need_distance and Ep1OccupancyApplyButton.SET_DISTANCE in buttons:
            client.button_command(int(buttons[Ep1OccupancyApplyButton.SET_DISTANCE].key))
            distance_applied = True
        if need_sensitivity and Ep1OccupancyApplyButton.SET_SENSITIVITY in buttons:
            client.button_command(int(buttons[Ep1OccupancyApplyButton.SET_SENSITIVITY].key))
            sensitivity_applied = True
        if distance_applied or sensitivity_applied:
            await asyncio.sleep(_POST_BUTTON_SETTLE_S)

        knobs_confirmed, write_states = await _wait_for_number_writes(
            client,
            expected_numbers={int(number.key): value for number, value in pending_writes},
            also_collect_keys={int(number.key) for number in kind_to_number.values()},
            host=target.host,
            port=target.port,
        )
        # The wait's subscribe already consumed ESPHome's one-shot state dump for this
        # connection — do not subscribe again for missing siblings.
        snapshot = await _snapshot_from_client(
            client,
            target=target,
            entities=entities,
            seed_states=write_states,
            collect_missing=False,
            require_complete=False,
        )
        return replace(
            snapshot,
            distance_applied=distance_applied,
            knobs_confirmed=knobs_confirmed,
            sensitivity_applied=sensitivity_applied,
        )
    except APIConnectionError as exc:
        raise Ep1OccupancyTuningError(
            f"EP1 occupancy tuning write failed at {target.host}:{target.port}: {exc}"
        ) from exc
    finally:
        await _disconnect_client(client)


async def read_ep1_occupancy_tuning(
    *,
    device_id: str,
    cache_path: Path | None = None,
    cli_noise_psk: str | None = None,
    ep1_mgr: Ep1DeviceManager | None = None,
) -> Ep1OccupancyTuningSnapshot:
    """Connect to ``device_id`` and read mmWave occupancy tuning numbers."""

    target = resolve_ep1_settings_target(
        device_id,
        cache_path=cache_path,
        ep1_mgr=ep1_mgr,
    )
    if target is None:
        raise Ep1OccupancyTuningNotFoundError(EP1_OCCUPANCY_TUNING_DEVICE_NOT_FOUND.format(device_id=device_id))

    psk = _resolved_noise_psk(cli_noise_psk=cli_noise_psk, cache_path=cache_path)
    client = _ep1_api_client(host=target.host, port=target.port, noise_psk=psk)
    try:
        await client.connect(login=True)
        entities, _services = await client.list_entities_services()
        return await _snapshot_from_client(client, target=target, entities=entities)
    except APIConnectionError as exc:
        raise Ep1OccupancyTuningError(
            f"EP1 occupancy tuning read failed at {target.host}:{target.port}: {exc}"
        ) from exc
    finally:
        await _disconnect_client(client)


def _button_entities_by_role(entities: Sequence[EntityInfo]) -> dict[Ep1OccupancyApplyButton, ButtonInfo]:
    out: dict[Ep1OccupancyApplyButton, ButtonInfo] = {}
    for entity in entities:
        if not isinstance(entity, ButtonInfo):
            continue
        role = _button_role_for_entity(entity)
        if role is None or role in out:
            continue
        out[role] = entity
    return out


def _button_role_for_entity(entity: ButtonInfo) -> Ep1OccupancyApplyButton | None:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    for role, aliases in _BUTTON_ALIASES.items():
        if tokens.intersection(aliases):
            return role
    return None


async def _collect_states_async(
    client: APIClient,
    keys: set[int],
    *,
    timeout_s: float = _STATE_COLLECT_TIMEOUT_S,
) -> dict[int, EntityState]:
    if not keys:
        return {}
    collected: dict[int, EntityState] = {}
    done = asyncio.Event()

    def _on_state(state: EntityState) -> None:
        if state.key not in keys:
            return
        collected[int(state.key)] = state
        if keys <= set(collected):
            done.set()

    client.subscribe_states(_on_state)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except TimeoutError:
        _LOGGER.debug(
            "EP1 occupancy tuning state collect timed out after %.1fs (got %s)",
            timeout_s,
            sorted(collected),
        )
    return collected


async def _disconnect_client(client: APIClient) -> None:
    try:
        await client.disconnect(force=True)
    except Exception:
        _LOGGER.debug("EP1 occupancy tuning client disconnect failed", exc_info=True)


def _distance_apply_button_required(entities: Sequence[EntityInfo]) -> bool:
    """Require Set Distance when any SEN0609 distance alias is present on the device."""

    if _entities_match_any_alias(entities, _SEN0609_DISTANCE_ALIASES):
        return True
    if _entities_match_any_alias(entities, _SEN0395_DISTANCE_ALIASES):
        return False
    return True


def _entities_match_any_alias(entities: Sequence[EntityInfo], aliases: frozenset[str]) -> bool:
    for entity in entities:
        if not isinstance(entity, (NumberInfo, ButtonInfo)):
            continue
        if _entity_tokens(entity) & aliases:
            return True
    return False


def _entity_tokens(entity: EntityInfo) -> set[str]:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    return tokens


def _ep1_api_client(*, host: str, port: int, noise_psk: str | None) -> APIClient:
    return APIClient(
        host,
        port,
        password=None,
        noise_psk=noise_psk,
        client_info="domesti-bot-ep1-occupancy-tuning",
    )


def _float_from_number_state(state: EntityState | None) -> float | None:
    if not isinstance(state, NumberState):
        return None
    if getattr(state, "missing_state", False):
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _knob_alias_rank(entity: NumberInfo, kind: Ep1OccupancyTuningKind) -> int:
    """Higher ranks win when multiple entities map to the same kind (prefer SEN0609)."""

    tokens = _entity_tokens(entity)
    if kind in _SENSITIVITY_KINDS:
        if tokens & _SEN0609_SENSITIVITY_ALIASES:
            return 2
        if tokens & _SEN0395_SENSITIVITY_ALIASES:
            return 1
    if kind in _DISTANCE_KINDS:
        if tokens & _SEN0609_DISTANCE_ALIASES:
            return 2
        if tokens & _SEN0395_DISTANCE_ALIASES:
            return 1
    return 0


def _knob_field_unavailable(kind: Ep1OccupancyTuningKind) -> Ep1OccupancyTuningField:
    return Ep1OccupancyTuningField(
        available=False,
        kind=kind,
        max_value=None,
        min_value=None,
        step=None,
        unit=None,
        value=None,
    )


def _knob_kind_for_number(entity: NumberInfo) -> Ep1OccupancyTuningKind | None:
    tokens = {
        _normalize_entity_token(getattr(entity, "name", "") or ""),
        _normalize_entity_token(getattr(entity, "object_id", "") or ""),
    }
    tokens.discard("")
    for kind, aliases in _KNOB_ENTITY_ALIASES.items():
        if tokens.intersection(aliases):
            return kind
    return None


def _normalize_entity_token(value: str) -> str:
    return value.strip().lower().replace(" ", "_").replace("-", "_")


def _number_entities_by_kind(
    entities: Sequence[EntityInfo],
) -> dict[Ep1OccupancyTuningKind, NumberInfo]:
    out: dict[Ep1OccupancyTuningKind, NumberInfo] = {}
    ranks: dict[Ep1OccupancyTuningKind, int] = {}
    for entity in entities:
        if not isinstance(entity, NumberInfo):
            continue
        kind = _knob_kind_for_number(entity)
        if kind is None:
            continue
        rank = _knob_alias_rank(entity, kind)
        if kind in out and ranks[kind] >= rank:
            continue
        out[kind] = entity
        ranks[kind] = rank
    return out


def _number_state_matches_expected(state: EntityState | None, expected: float) -> bool:
    actual = _float_from_number_state(state)
    if actual is None:
        return False
    return abs(actual - expected) <= max(_NUMBER_VALUE_ABS_TOL, abs(expected) * 1.2e-7)


def _requested_knob_updates(
    *,
    max_distance: float | None,
    min_distance: float | None,
    off_latency: float | None,
    on_latency: float | None,
    sustain_sensitivity: float | None,
    trigger_distance: float | None,
    trigger_sensitivity: float | None,
) -> dict[Ep1OccupancyTuningKind, float]:
    updates: dict[Ep1OccupancyTuningKind, float] = {}
    if max_distance is not None:
        updates[Ep1OccupancyTuningKind.MAX_DISTANCE] = float(max_distance)
    if min_distance is not None:
        updates[Ep1OccupancyTuningKind.MIN_DISTANCE] = float(min_distance)
    if off_latency is not None:
        updates[Ep1OccupancyTuningKind.OFF_LATENCY] = float(off_latency)
    if on_latency is not None:
        updates[Ep1OccupancyTuningKind.ON_LATENCY] = float(on_latency)
    if sustain_sensitivity is not None:
        updates[Ep1OccupancyTuningKind.SUSTAIN_SENSITIVITY] = float(sustain_sensitivity)
    if trigger_distance is not None:
        updates[Ep1OccupancyTuningKind.TRIGGER_DISTANCE] = float(trigger_distance)
    if trigger_sensitivity is not None:
        updates[Ep1OccupancyTuningKind.TRIGGER_SENSITIVITY] = float(trigger_sensitivity)
    return updates


def _resolved_noise_psk(*, cli_noise_psk: str | None, cache_path: Path | None) -> str | None:
    psk, _source = resolve_ep1_noise_psk(cli_psk=cli_noise_psk, cache_path=cache_path)
    return (psk or "").strip() or None


def _sensitivity_apply_button_required(entities: Sequence[EntityInfo]) -> bool:
    """Require Set Sensitivity when any SEN0609 sensitivity alias is present on the device."""

    if _entities_match_any_alias(entities, _SEN0609_SENSITIVITY_ALIASES):
        return True
    if _entities_match_any_alias(entities, _SEN0395_SENSITIVITY_ALIASES):
        return False
    return True


async def _snapshot_from_client(
    client: APIClient,
    *,
    target: Ep1SettingsTarget,
    entities: Sequence[EntityInfo],
    seed_states: Mapping[int, EntityState] | None = None,
    collect_missing: bool = True,
    require_complete: bool = True,
) -> Ep1OccupancyTuningSnapshot:
    numbers = _number_entities_by_kind(entities)
    keys = {int(number.key) for number in numbers.values()}
    states = dict(seed_states or {})
    missing = keys - set(states)
    if missing and collect_missing:
        states.update(await _collect_states_async(client, missing))
    if require_complete:
        if keys and not states:
            raise Ep1OccupancyTuningError(EP1_OCCUPANCY_TUNING_STATE_TIMEOUT.format(host=target.host, port=target.port))
        if keys - set(states):
            raise Ep1OccupancyTuningError(
                EP1_OCCUPANCY_TUNING_STATE_INCOMPLETE.format(
                    host=target.host,
                    port=target.port,
                    got=len(states),
                    expected=len(keys),
                )
            )

    knobs: dict[Ep1OccupancyTuningKind, Ep1OccupancyTuningField] = {}
    for kind in Ep1OccupancyTuningKind:
        number = numbers.get(kind)
        if number is None:
            knobs[kind] = _knob_field_unavailable(kind)
            continue
        knobs[kind] = Ep1OccupancyTuningField(
            available=True,
            kind=kind,
            max_value=float(number.max_value),
            min_value=float(number.min_value),
            step=float(number.step),
            unit=(number.unit_of_measurement or "").strip() or None,
            value=_float_from_number_state(states.get(int(number.key))),
        )
    return Ep1OccupancyTuningSnapshot(
        device_id=target.device_id,
        display_label=target.display_label,
        display_name=target.display_name,
        host=target.host,
        knobs=knobs,
        port=target.port,
    )


def _validate_knob_in_range(
    *,
    kind: Ep1OccupancyTuningKind,
    number: NumberInfo,
    value: float,
) -> None:
    lo = float(number.min_value)
    hi = float(number.max_value)
    if not math.isfinite(value):
        raise Ep1OccupancyTuningValidationError(f"Expected a finite {kind.value} value, got {value!r}")
    if value < lo or value > hi:
        raise Ep1OccupancyTuningValidationError(f"Expected {kind.value} in [{lo}, {hi}], got {value}")
    step = float(number.step)
    if step > 0 and value not in (lo, hi):
        steps_from_min = (value - lo) / step
        nearest = round(steps_from_min)
        if abs(steps_from_min - nearest) > max(_NUMBER_VALUE_ABS_TOL / step, 1e-9):
            raise Ep1OccupancyTuningValidationError(
                f"Expected {kind.value} aligned to step {step} from {lo}, got {value}"
            )


async def _wait_for_number_writes(
    client: APIClient,
    *,
    expected_numbers: Mapping[int, float],
    also_collect_keys: set[int] | None = None,
    host: str = "",
    port: int = 0,
    timeout_s: float = _POST_WRITE_TIMEOUT_S,
) -> tuple[bool, dict[int, EntityState]]:
    """Wait until post-write number states match expected values.

    Always subscribes after the write (never short-circuits on a pre-write
    baseline). Collects any keys in ``also_collect_keys`` from the same
    subscribe dump so a later snapshot does not need a second subscription.
    Returns ``(confirmed, collected_states)``; on soft-timeout ``confirmed`` is
    False so callers can still build a snapshot.

    Empty ``expected_numbers`` returns ``(True, {})`` as a defensive contract for
    future callers; ``apply_ep1_occupancy_tuning`` never reaches that path today.
    """

    if not expected_numbers:
        return True, {}

    collect_keys = set(expected_numbers) | set(also_collect_keys or ())
    confirmed: set[int] = set()
    collected: dict[int, EntityState] = {}
    done = asyncio.Event()

    def _on_state(state: EntityState) -> None:
        key = int(state.key)
        if key in collect_keys:
            collected[key] = state
        if key in expected_numbers and _number_state_matches_expected(state, expected_numbers[key]):
            confirmed.add(key)
        if confirmed >= set(expected_numbers) and collect_keys <= set(collected):
            done.set()

    client.subscribe_states(_on_state)
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout_s)
    except TimeoutError:
        if confirmed >= set(expected_numbers):
            _LOGGER.debug(
                "EP1 occupancy tuning post-write: writes confirmed, sibling states incomplete "
                "(collected=%s host=%s port=%s)",
                sorted(collected),
                host or "?",
                port or "?",
            )
            return True, collected
        _LOGGER.info(
            "EP1 occupancy tuning post-write: numbers not confirmed after %.1fs "
            "(confirmed=%s expected=%s host=%s port=%s)",
            timeout_s,
            sorted(confirmed),
            sorted(expected_numbers),
            host or "?",
            port or "?",
        )
        return False, collected
    return True, collected
