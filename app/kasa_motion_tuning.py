"""Kasa wall-switch PIR / ambient light tuning via python-kasa modules.

Settings reads and writes use the live :class:`~app.kasa_device_manager.KasaDeviceManager`
(connected ``Device`` instances). Capability is detected from the ``motion`` module
(``Module.IotMotion``), not a hard-coded model string — KS200M is the household
example today.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from kasa.device import Device as KDevice
from kasa.iot.modules.ambientlight import AmbientLight
from kasa.iot.modules.motion import Motion
from kasa.module import Module

from app.device_display import format_device_display
from app.device_enums import KasaPirRange
from app.device_mac import try_normalize_mac
from app.device_manager import NotInitializedError
from app.kasa_device_manager import KasaDevice, KasaDeviceManager

_LOGGER = logging.getLogger(__name__)

KASA_MOTION_TUNING_DEVICE_NOT_FOUND = "No Kasa motion device matched device_id={device_id!r}"
KASA_MOTION_TUNING_INACTIVITY_TIMEOUT_RANGE = "Expected inactivity_timeout_ms >= 0, got {value}"
KASA_MOTION_TUNING_MANAGER_UNAVAILABLE = (
    "Kasa device manager is not available; wait for discovery or check Kasa Settings"
)
KASA_MOTION_TUNING_MODULE_UNAVAILABLE = "Kasa device {display} has no motion (PIR) module after update"
KASA_MOTION_TUNING_THRESHOLD_RANGE = "Expected pir_threshold in [0, 100], got {value}"
_INACTIVITY_TIMEOUT_MS_MIN = 0
_PIR_THRESHOLD_MAX = 100
_PIR_THRESHOLD_MIN = 0


class KasaMotionTuningError(ValueError):
    """Operator-facing motion tuning failure (maps to HTTP 4xx/502)."""


class KasaMotionTuningNotFoundError(KasaMotionTuningError):
    """``device_id`` does not match a known motion-capable Kasa switch."""


class KasaMotionTuningValidationError(KasaMotionTuningError):
    """Knob out of range or ambient write requested without ambient module."""


@dataclass(frozen=True, slots=True)
class KasaMotionSettingsTarget:
    """One motion-capable Kasa switch for Settings → Target device."""

    device_id: str
    display_label: str
    display_name: str | None
    host: str
    model: str | None


@dataclass(frozen=True, slots=True)
class KasaMotionTuningSnapshot:
    """PIR / ambient config + live sensors for one Kasa switch."""

    ambient_available: bool
    ambient_light: int | None
    ambient_light_enabled: bool | None
    device_id: str
    display_label: str
    display_name: str | None
    host: str
    inactivity_timeout_ms: int
    model: str | None
    pir_enabled: bool
    pir_percent: float | None
    pir_range: KasaPirRange
    pir_range_choices: tuple[KasaPirRange, ...]
    pir_threshold: int
    pir_triggered: bool
    knobs_confirmed: bool = True


async def apply_kasa_motion_tuning(
    *,
    device_id: str,
    ambient_light_enabled: bool | None = None,
    inactivity_timeout_ms: int | None = None,
    kasa_mgr: KasaDeviceManager | None,
    pir_enabled: bool | None = None,
    pir_range: KasaPirRange | None = None,
    pir_threshold: int | None = None,
) -> KasaMotionTuningSnapshot:
    """Write PIR / ambient config knobs on ``device_id``, then return a fresh snapshot."""

    if kasa_mgr is None:
        raise KasaMotionTuningError(KASA_MOTION_TUNING_MANAGER_UNAVAILABLE)

    kd = _resolve_motion_device(device_id, kasa_mgr=kasa_mgr)
    if kd is None:
        raise KasaMotionTuningNotFoundError(KASA_MOTION_TUNING_DEVICE_NOT_FOUND.format(device_id=device_id))

    updates_requested = any(
        value is not None
        for value in (
            ambient_light_enabled,
            inactivity_timeout_ms,
            pir_enabled,
            pir_range,
            pir_threshold,
        )
    )
    if not updates_requested:
        return await read_kasa_motion_tuning(device_id=device_id, kasa_mgr=kasa_mgr)

    if inactivity_timeout_ms is not None and inactivity_timeout_ms < _INACTIVITY_TIMEOUT_MS_MIN:
        raise KasaMotionTuningValidationError(
            KASA_MOTION_TUNING_INACTIVITY_TIMEOUT_RANGE.format(value=inactivity_timeout_ms)
        )

    if pir_threshold is not None and not (_PIR_THRESHOLD_MIN <= pir_threshold <= _PIR_THRESHOLD_MAX):
        raise KasaMotionTuningValidationError(KASA_MOTION_TUNING_THRESHOLD_RANGE.format(value=pir_threshold))

    try:
        await kd._kDevice.update()
    except Exception as exc:
        raise KasaMotionTuningError(
            f"Failed to update Kasa device {format_device_display(kd.identifier, kd.preferred_label)}: {exc!r}"
        ) from exc

    motion = _motion_module(kd._kDevice)
    if motion is None:
        raise KasaMotionTuningError(
            KASA_MOTION_TUNING_MODULE_UNAVAILABLE.format(
                display=format_device_display(kd.identifier, kd.preferred_label)
            )
        )

    ambient = _ambient_module(kd._kDevice)
    if ambient_light_enabled is not None and ambient is None:
        raise KasaMotionTuningValidationError(
            f"Kasa device {format_device_display(kd.identifier, kd.preferred_label)} "
            "has no ambient light module; cannot set ambient_light_enabled"
        )

    try:
        if pir_enabled is not None:
            await motion.set_enabled(pir_enabled)
        # ``set_threshold`` always writes the Custom range index — skip a
        # concurrent range preset write so we do not claim Near/Far stuck.
        if pir_threshold is not None:
            await motion.set_threshold(pir_threshold)
        elif pir_range is not None:
            await motion._set_range_from_str(pir_range.value)
        if inactivity_timeout_ms is not None:
            await motion.set_inactivity_timeout(inactivity_timeout_ms)
        if ambient_light_enabled is not None and ambient is not None:
            await ambient.set_enabled(ambient_light_enabled)
        await kd._kDevice.update()
    except KasaMotionTuningError:
        raise
    except Exception as exc:
        raise KasaMotionTuningError(
            f"Failed to apply Kasa motion tuning on {format_device_display(kd.identifier, kd.preferred_label)}: {exc!r}"
        ) from exc

    effective_range = None if pir_threshold is not None else pir_range
    snapshot = _snapshot_from_device(kd)
    confirmed = _knobs_match_request(
        snapshot,
        ambient_light_enabled=ambient_light_enabled,
        inactivity_timeout_ms=inactivity_timeout_ms,
        pir_enabled=pir_enabled,
        pir_range=effective_range,
        pir_threshold=pir_threshold,
    )
    if confirmed:
        return snapshot
    return KasaMotionTuningSnapshot(
        ambient_available=snapshot.ambient_available,
        ambient_light=snapshot.ambient_light,
        ambient_light_enabled=snapshot.ambient_light_enabled,
        device_id=snapshot.device_id,
        display_label=snapshot.display_label,
        display_name=snapshot.display_name,
        host=snapshot.host,
        inactivity_timeout_ms=snapshot.inactivity_timeout_ms,
        knobs_confirmed=False,
        model=snapshot.model,
        pir_enabled=snapshot.pir_enabled,
        pir_percent=snapshot.pir_percent,
        pir_range=snapshot.pir_range,
        pir_range_choices=snapshot.pir_range_choices,
        pir_threshold=snapshot.pir_threshold,
        pir_triggered=snapshot.pir_triggered,
    )


def device_has_kasa_motion(dev: KDevice) -> bool:
    """Return True when python-kasa registered the IoT motion (PIR) module."""

    return Module.IotMotion in dev.modules


def list_kasa_motion_settings_targets(
    *,
    kasa_mgr: KasaDeviceManager | None,
) -> list[KasaMotionSettingsTarget]:
    """Return live Kasa switches that expose a motion module, sorted by display label."""

    if kasa_mgr is None:
        return []
    rows: list[KasaMotionSettingsTarget] = []
    for kd in _live_switches(kasa_mgr):
        if not device_has_kasa_motion(kd._kDevice):
            continue
        display_name = kd.preferred_label
        rows.append(
            KasaMotionSettingsTarget(
                device_id=kd.identifier,
                display_label=format_device_display(kd.identifier, display_name),
                display_name=display_name,
                host=kd.host,
                model=_device_model(kd._kDevice),
            )
        )
    rows.sort(key=lambda row: (row.display_label.casefold(), row.device_id))
    return rows


async def read_kasa_motion_tuning(
    *,
    device_id: str,
    kasa_mgr: KasaDeviceManager | None,
) -> KasaMotionTuningSnapshot:
    """Refresh and read PIR / ambient knobs + sensors for ``device_id``."""

    if kasa_mgr is None:
        raise KasaMotionTuningError(KASA_MOTION_TUNING_MANAGER_UNAVAILABLE)

    kd = _resolve_motion_device(device_id, kasa_mgr=kasa_mgr)
    if kd is None:
        raise KasaMotionTuningNotFoundError(KASA_MOTION_TUNING_DEVICE_NOT_FOUND.format(device_id=device_id))

    try:
        await kd._kDevice.update()
    except Exception as exc:
        raise KasaMotionTuningError(
            f"Failed to update Kasa device {format_device_display(kd.identifier, kd.preferred_label)}: {exc!r}"
        ) from exc

    if _motion_module(kd._kDevice) is None:
        raise KasaMotionTuningError(
            KASA_MOTION_TUNING_MODULE_UNAVAILABLE.format(
                display=format_device_display(kd.identifier, kd.preferred_label)
            )
        )
    return _snapshot_from_device(kd)


def _ambient_module(dev: KDevice) -> AmbientLight | None:
    module = dev.modules.get(Module.IotAmbientLight)
    if module is None:
        return None
    if not isinstance(module, AmbientLight):
        _LOGGER.warning("Unexpected ambient module type for %s: %r", getattr(dev, "host", None), type(module))
        return None
    return module


def _device_model(dev: KDevice) -> str | None:
    model = getattr(dev, "model", None)
    if isinstance(model, str) and model.strip():
        return model.strip()
    sys_info = getattr(dev, "sys_info", None)
    if isinstance(sys_info, dict):
        raw = sys_info.get("model")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _knobs_match_request(
    snapshot: KasaMotionTuningSnapshot,
    *,
    ambient_light_enabled: bool | None,
    inactivity_timeout_ms: int | None,
    pir_enabled: bool | None,
    pir_range: KasaPirRange | None,
    pir_threshold: int | None,
) -> bool:
    if pir_enabled is not None and snapshot.pir_enabled is not pir_enabled:
        return False
    if pir_threshold is not None and snapshot.pir_threshold != pir_threshold:
        return False
    if pir_range is not None and snapshot.pir_range is not pir_range:
        return False
    if inactivity_timeout_ms is not None and snapshot.inactivity_timeout_ms != inactivity_timeout_ms:
        return False
    if ambient_light_enabled is not None:
        if snapshot.ambient_light_enabled is not ambient_light_enabled:
            return False
    return True


def _live_switches(kasa_mgr: KasaDeviceManager) -> tuple[KasaDevice, ...]:
    """Return discovered switches, or ``()`` when Kasa bootstrap left the manager uninitialized."""

    try:
        return kasa_mgr.switches
    except NotInitializedError:
        return ()


def _motion_module(dev: KDevice) -> Motion | None:
    module = dev.modules.get(Module.IotMotion)
    if module is None:
        return None
    if not isinstance(module, Motion):
        _LOGGER.warning("Unexpected motion module type for %s: %r", getattr(dev, "host", None), type(module))
        return None
    return module


def _parse_pir_range(raw: object) -> KasaPirRange:
    if isinstance(raw, KasaPirRange):
        return raw
    name = getattr(raw, "name", None)
    if isinstance(name, str):
        try:
            return KasaPirRange(name)
        except ValueError:
            pass
    if isinstance(raw, str):
        try:
            return KasaPirRange(raw.strip().capitalize() if raw.strip().islower() else raw.strip())
        except ValueError:
            # Choice Feature may already be "Mid" / "Far" / …
            try:
                return KasaPirRange(raw)
            except ValueError as exc:
                raise KasaMotionTuningError(f"Expected pir_range in {list(KasaPirRange)}, got {raw!r}") from exc
    raise KasaMotionTuningError(f"Expected pir_range in {list(KasaPirRange)}, got {raw!r}")


def _parse_pir_range_choices(raw_choices: Sequence[str] | None) -> tuple[KasaPirRange, ...]:
    if raw_choices is None or len(raw_choices) == 0:
        # Device did not advertise choices — fall back to the closed set.
        return tuple(KasaPirRange)
    parsed: list[KasaPirRange] = []
    for item in raw_choices:
        parsed.append(_parse_pir_range(item))
    return tuple(parsed)


def _resolve_motion_device(
    device_id: str,
    *,
    kasa_mgr: KasaDeviceManager,
) -> KasaDevice | None:
    mac = try_normalize_mac(device_id)
    if mac is None:
        return None
    for kd in _live_switches(kasa_mgr):
        if kd.identifier != mac:
            continue
        if not device_has_kasa_motion(kd._kDevice):
            return None
        return kd
    return None


def _snapshot_from_device(kd: KasaDevice) -> KasaMotionTuningSnapshot:
    motion = _motion_module(kd._kDevice)
    if motion is None:
        raise KasaMotionTuningError(
            KASA_MOTION_TUNING_MODULE_UNAVAILABLE.format(
                display=format_device_display(kd.identifier, kd.preferred_label)
            )
        )
    ambient = _ambient_module(kd._kDevice)
    display_name = kd.preferred_label
    display = format_device_display(kd.identifier, display_name)
    try:
        pir_range = _parse_pir_range(motion.range)
        raw_ranges = motion.ranges
        choices = _parse_pir_range_choices(list(raw_ranges) if raw_ranges is not None else None)
        pir_enabled = bool(motion.enabled)
        pir_percent = float(motion.pir_percent)
        pir_threshold = int(motion.threshold)
        pir_triggered = bool(motion.pir_triggered)
        raw_timeout = motion.inactivity_timeout
        # python-kasa leaves this unset until cold_time is present in get_config.
        inactivity_timeout_ms = 0 if raw_timeout is None else int(raw_timeout)
    except KasaMotionTuningError:
        raise
    except Exception as exc:
        raise KasaMotionTuningError(f"Failed reading Kasa motion state on {display}: {exc!r}") from exc
    ambient_enabled: bool | None = None
    ambient_light: int | None = None
    if ambient is not None:
        try:
            ambient_enabled = bool(ambient.enabled)
            ambient_light = int(ambient.ambientlight_brightness)
        except Exception as exc:
            _LOGGER.warning(
                "Failed reading ambient sensors on %s: %r",
                kd.host,
                exc,
            )
    return KasaMotionTuningSnapshot(
        ambient_available=ambient is not None,
        ambient_light=ambient_light,
        ambient_light_enabled=ambient_enabled,
        device_id=kd.identifier,
        display_label=display,
        display_name=display_name,
        host=kd.host,
        inactivity_timeout_ms=inactivity_timeout_ms,
        model=_device_model(kd._kDevice),
        pir_enabled=pir_enabled,
        pir_percent=pir_percent,
        pir_range=pir_range,
        pir_range_choices=choices,
        pir_threshold=pir_threshold,
        pir_triggered=pir_triggered,
    )
