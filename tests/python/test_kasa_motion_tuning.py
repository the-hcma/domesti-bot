"""Hermetic unit tests for Kasa PIR / ambient motion tuning."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from kasa.iot.modules.ambientlight import AmbientLight
from kasa.iot.modules.motion import Motion, Range
from kasa.module import Module

from app.device_enums import KasaPirRange
from app.kasa_device_manager import KasaDevice
from app.kasa_motion_tuning import (
    KASA_MOTION_TUNING_BRIGHTNESS_LIMIT_RANGE,
    KASA_MOTION_TUNING_DEVICE_NOT_FOUND,
    KASA_MOTION_TUNING_INACTIVITY_TIMEOUT_RANGE,
    KASA_MOTION_TUNING_THRESHOLD_RANGE,
    KasaAmbientBrightnessPreset,
    KasaMotionTuningNotFoundError,
    KasaMotionTuningValidationError,
    apply_kasa_motion_tuning,
    device_has_kasa_motion,
    list_kasa_motion_settings_targets,
    read_kasa_motion_tuning,
)


def test_device_has_kasa_motion_requires_iot_motion_module() -> None:
    with_motion = MagicMock()
    with_motion.modules = {Module.IotMotion: object()}
    without = MagicMock()
    without.modules = {}
    assert device_has_kasa_motion(with_motion) is True
    assert device_has_kasa_motion(without) is False


def test_list_kasa_motion_settings_targets_filters_and_sorts() -> None:
    garage = _fake_kasa_device(
        mac="98:25:4a:64:ac:90",
        alias="Garage light",
        host="192.168.86.186",
        model="KS200M(US)",
        has_motion=True,
    )
    basement = _fake_kasa_device(
        mac="aa:bb:cc:dd:ee:01",
        alias="Basement leds",
        host="192.168.86.187",
        model="KS200(US)",
        has_motion=False,
    )
    attic = _fake_kasa_device(
        mac="aa:bb:cc:dd:ee:02",
        alias="Attic PIR",
        host="192.168.86.10",
        model="KS200M(US)",
        has_motion=True,
    )
    mgr = SimpleNamespace(switches=(garage, basement, attic))
    rows = list_kasa_motion_settings_targets(kasa_mgr=mgr)  # type: ignore[arg-type]
    assert [row.device_id for row in rows] == ["aa:bb:cc:dd:ee:02", "98:25:4a:64:ac:90"]
    assert rows[0].display_label.startswith("Attic PIR")
    assert rows[1].model == "KS200M(US)"


def test_list_kasa_motion_settings_targets_empty_without_manager() -> None:
    assert list_kasa_motion_settings_targets(kasa_mgr=None) == []


def test_list_kasa_motion_settings_targets_tolerates_uninitialized_manager() -> None:
    from app.device_manager import NotInitializedError

    mgr = MagicMock()
    type(mgr).switches = property(lambda self: (_ for _ in ()).throw(NotInitializedError()))
    assert list_kasa_motion_settings_targets(kasa_mgr=mgr) == []


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_writes_knobs() -> None:
    kd, motion, ambient = _fake_motion_device()
    assert ambient is not None
    mgr = SimpleNamespace(switches=(kd,))
    snap = await apply_kasa_motion_tuning(
        device_id=kd.identifier,
        kasa_mgr=mgr,  # type: ignore[arg-type]
        pir_enabled=False,
        pir_range=KasaPirRange.NEAR,
        pir_threshold=40,
        inactivity_timeout_ms=120_000,
        ambient_light_enabled=False,
        ambient_brightness_limit=11,
    )
    motion.set_enabled.assert_awaited_once_with(False)
    motion._set_range_from_str.assert_not_awaited()
    motion.set_threshold.assert_awaited_once_with(40)
    motion.set_inactivity_timeout.assert_awaited_once_with(120_000)
    ambient.set_enabled.assert_awaited_once_with(False)
    ambient.set_brightness_limit.assert_awaited_once_with(11)
    assert snap.knobs_confirmed is True
    assert snap.pir_enabled is False
    # Threshold write forces Custom; concurrent Near is skipped on purpose.
    assert snap.pir_range is KasaPirRange.CUSTOM
    assert snap.pir_threshold == 40
    assert snap.inactivity_timeout_ms == 120_000
    assert snap.ambient_light_enabled is False
    assert snap.ambient_brightness_limit == 11


@pytest.mark.asyncio
async def test_apply_range_only_writes_preset() -> None:
    kd, motion, ambient = _fake_motion_device()
    mgr = SimpleNamespace(switches=(kd,))
    snap = await apply_kasa_motion_tuning(
        device_id=kd.identifier,
        kasa_mgr=mgr,  # type: ignore[arg-type]
        pir_range=KasaPirRange.NEAR,
    )
    motion._set_range_from_str.assert_awaited_once_with("Near")
    motion.set_threshold.assert_not_awaited()
    motion.set_inactivity_timeout.assert_not_awaited()
    assert snap.pir_range is KasaPirRange.NEAR
    assert snap.knobs_confirmed is True
    del ambient


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_rejects_bad_brightness_limit() -> None:
    kd, _motion, _ambient = _fake_motion_device()
    mgr = SimpleNamespace(switches=(kd,))
    with pytest.raises(KasaMotionTuningValidationError) as exc_info:
        await apply_kasa_motion_tuning(
            device_id=kd.identifier,
            kasa_mgr=mgr,  # type: ignore[arg-type]
            ambient_brightness_limit=-1,
        )
    assert str(exc_info.value) == KASA_MOTION_TUNING_BRIGHTNESS_LIMIT_RANGE.format(value=-1)


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_rejects_bad_inactivity_timeout() -> None:
    kd, _motion, _ambient = _fake_motion_device()
    mgr = SimpleNamespace(switches=(kd,))
    with pytest.raises(KasaMotionTuningValidationError) as exc_info:
        await apply_kasa_motion_tuning(
            device_id=kd.identifier,
            kasa_mgr=mgr,  # type: ignore[arg-type]
            inactivity_timeout_ms=-1,
        )
    assert str(exc_info.value) == KASA_MOTION_TUNING_INACTIVITY_TIMEOUT_RANGE.format(value=-1)


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_rejects_bad_threshold() -> None:
    kd, _motion, _ambient = _fake_motion_device()
    mgr = SimpleNamespace(switches=(kd,))
    with pytest.raises(KasaMotionTuningValidationError, match="pir_threshold"):
        await apply_kasa_motion_tuning(
            device_id=kd.identifier,
            kasa_mgr=mgr,  # type: ignore[arg-type]
            pir_threshold=101,
        )
    assert KASA_MOTION_TUNING_THRESHOLD_RANGE.format(value=101)


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_rejects_ambient_without_module() -> None:
    kd, motion, ambient = _fake_motion_device(with_ambient=False)
    assert ambient is None
    mgr = SimpleNamespace(switches=(kd,))
    with pytest.raises(KasaMotionTuningValidationError, match="ambient"):
        await apply_kasa_motion_tuning(
            device_id=kd.identifier,
            kasa_mgr=mgr,  # type: ignore[arg-type]
            ambient_light_enabled=True,
        )
    motion.set_enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_kasa_motion_tuning_rejects_brightness_limit_without_module() -> None:
    kd, motion, ambient = _fake_motion_device(with_ambient=False)
    assert ambient is None
    mgr = SimpleNamespace(switches=(kd,))
    with pytest.raises(KasaMotionTuningValidationError, match="ambient"):
        await apply_kasa_motion_tuning(
            device_id=kd.identifier,
            kasa_mgr=mgr,  # type: ignore[arg-type]
            ambient_brightness_limit=15,
        )
    motion.set_enabled.assert_not_awaited()


@pytest.mark.asyncio
async def test_apply_marks_unconfirmed_when_device_does_not_stick() -> None:
    kd, motion, ambient = _fake_motion_device()
    # Keep reading Mid/50 even after Near/40 writes.
    motion.enabled = True
    motion.range = Range.Mid
    motion.threshold = 50
    motion._set_range_from_str = AsyncMock()
    motion.set_threshold = AsyncMock()
    mgr = SimpleNamespace(switches=(kd,))
    snap = await apply_kasa_motion_tuning(
        device_id=kd.identifier,
        kasa_mgr=mgr,  # type: ignore[arg-type]
        pir_range=KasaPirRange.NEAR,
        pir_threshold=40,
    )
    assert snap.knobs_confirmed is False
    assert snap.pir_range is KasaPirRange.MID
    del ambient


@pytest.mark.asyncio
async def test_read_kasa_motion_tuning_defaults_missing_inactivity_timeout() -> None:
    kd, motion, ambient = _fake_motion_device()
    motion.inactivity_timeout = None
    mgr = SimpleNamespace(switches=(kd,))
    snap = await read_kasa_motion_tuning(device_id=kd.identifier, kasa_mgr=mgr)  # type: ignore[arg-type]
    assert snap.inactivity_timeout_ms == 0
    del ambient


@pytest.mark.asyncio
async def test_read_kasa_motion_tuning_returns_snapshot() -> None:
    kd, motion, ambient = _fake_motion_device()
    mgr = SimpleNamespace(switches=(kd,))
    snap = await read_kasa_motion_tuning(device_id=kd.identifier, kasa_mgr=mgr)  # type: ignore[arg-type]
    assert snap.device_id == "98:25:4a:64:ac:90"
    assert snap.pir_enabled is True
    assert snap.pir_range is KasaPirRange.MID
    assert snap.pir_threshold == 50
    assert snap.inactivity_timeout_ms == 60_000
    assert snap.pir_triggered is False
    assert snap.pir_value == -22
    assert snap.adc_min == 0
    assert snap.adc_mid == 2047
    assert snap.adc_max == 4095
    assert snap.adc_value == 2000
    assert snap.ambient_available is True
    assert snap.ambient_light_enabled is True
    assert snap.ambient_light == 64
    assert snap.ambient_brightness_limit == 15
    assert snap.ambient_brightness_limit_presets == (
        KasaAmbientBrightnessPreset(name="cloudy", value=15),
        KasaAmbientBrightnessPreset(name="overcast", value=11),
        KasaAmbientBrightnessPreset(name="dawn", value=8),
        KasaAmbientBrightnessPreset(name="custom", value=94),
    )
    assert KasaPirRange.MID in snap.pir_range_choices
    update = kd._kDevice.update
    assert isinstance(update, AsyncMock)
    update.assert_awaited()
    del motion, ambient


@pytest.mark.asyncio
async def test_read_kasa_motion_tuning_unknown_device() -> None:
    mgr = SimpleNamespace(switches=())
    with pytest.raises(KasaMotionTuningNotFoundError, match="aa:bb:cc:dd:ee:ff"):
        await read_kasa_motion_tuning(device_id="aa:bb:cc:dd:ee:ff", kasa_mgr=mgr)  # type: ignore[arg-type]


def test_not_found_message_constant() -> None:
    assert "device_id=" in KASA_MOTION_TUNING_DEVICE_NOT_FOUND


def _fake_kasa_device(
    *,
    mac: str,
    alias: str,
    host: str,
    model: str,
    has_motion: bool,
) -> KasaDevice:
    modules: dict[Any, Any] = {}
    if has_motion:
        modules[Module.IotMotion] = object()
    k_device = MagicMock()
    k_device.host = host
    k_device.model = model
    k_device.modules = modules
    k_device.is_on = False
    return KasaDevice(mac, k_device, display_name=alias, mac_address=mac)


def _fake_motion_device(*, with_ambient: bool = True) -> tuple[KasaDevice, MagicMock, MagicMock | None]:
    motion = MagicMock(spec=Motion)
    motion.enabled = True
    motion.range = Range.Mid
    motion.ranges = ["Far", "Mid", "Near", "Custom"]
    motion.threshold = 50
    motion.inactivity_timeout = 60_000
    motion.pir_triggered = False
    motion.pir_percent = -1.07
    motion.pir_value = -22
    motion.adc_min = 0
    motion.adc_mid = 2047
    motion.adc_max = 4095
    motion.adc_value = 2000
    motion.set_enabled = AsyncMock()
    motion._set_range_from_str = AsyncMock()
    motion.set_threshold = AsyncMock()
    motion.set_inactivity_timeout = AsyncMock()

    ambient: MagicMock | None = None
    modules: dict[Any, Any] = {Module.IotMotion: motion}
    if with_ambient:
        ambient = MagicMock(spec=AmbientLight)
        ambient.enabled = True
        ambient.ambientlight_brightness = 64
        ambient.presets = [
            {"adc": 390, "name": "cloudy", "value": 15},
            {"adc": 300, "name": "overcast", "value": 11},
            {"adc": 222, "name": "dawn", "value": 8},
            {"adc": 2400, "name": "custom", "value": 94},
        ]
        ambient.config = {"dark_index": 0, "enable": 1, "level_array": ambient.presets}
        ambient.set_enabled = AsyncMock()
        ambient.set_brightness_limit = AsyncMock()
        modules[Module.IotAmbientLight] = ambient

    k_device = MagicMock()
    k_device.host = "192.168.86.186"
    k_device.model = "KS200M(US)"
    k_device.modules = modules
    k_device.is_on = False
    k_device.update = AsyncMock()

    # After writes, reflect requested values so knobs_confirmed can pass.
    async def _set_enabled(state: bool) -> dict[str, Any]:
        motion.enabled = state
        return {}

    async def _set_range_from_str(value: str) -> dict[str, Any]:
        motion.range = Range[value]
        return {}

    async def _set_threshold(value: int) -> dict[str, Any]:
        motion.threshold = value
        motion.range = Range.Custom
        return {}

    async def _set_inactivity_timeout(timeout: int) -> dict[str, Any]:
        motion.inactivity_timeout = timeout
        return {}

    motion.set_enabled.side_effect = _set_enabled
    motion._set_range_from_str.side_effect = _set_range_from_str
    motion.set_threshold.side_effect = _set_threshold
    motion.set_inactivity_timeout.side_effect = _set_inactivity_timeout
    if ambient is not None:

        async def _set_ambient(state: bool) -> dict[str, Any]:
            ambient.enabled = state
            return {}

        async def _set_brightness_limit(value: int) -> dict[str, Any]:
            # Mimic selecting/updating the custom preset slot used by dark_index.
            ambient.presets = [
                *ambient.presets[:-1],
                {"adc": 2400, "name": "custom", "value": value},
            ]
            ambient.config = {
                "dark_index": len(ambient.presets) - 1,
                "enable": int(ambient.enabled),
                "level_array": ambient.presets,
            }
            return {}

        ambient.set_enabled.side_effect = _set_ambient
        ambient.set_brightness_limit.side_effect = _set_brightness_limit

    kd = KasaDevice(
        "98:25:4a:64:ac:90",
        k_device,
        display_name="Garage light",
        mac_address="98:25:4a:64:ac:90",
    )
    return kd, motion, ambient
