"""Hermetic tests for expected device-change attribution."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.schemas import RuleDeviceActionOut
from app.device_enums import DeviceFamilyId, RuleDeviceActionType
from app.expected_device_change import (
    DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S,
    consume_expected_device_change,
    expected_device_changes,
    is_expected_device_change,
    mark_expected_device_change,
    resolve_expected_device_change_window_s,
)
from app.rule_actions import dispatch_device_action
from app.ui_device_actions import flip_ui_device


def test_consume_expected_device_change_false_when_unmarked() -> None:
    expected_device_changes.clear()
    assert consume_expected_device_change(DeviceFamilyId.KASA, "lamp.local") is False


def test_is_expected_device_change_false_when_unmarked() -> None:
    expected_device_changes.clear()
    assert is_expected_device_change(DeviceFamilyId.KASA, "lamp.local") is False


def test_mark_then_is_expected_until_window_expires() -> None:
    expected_device_changes.clear()
    mark_expected_device_change(
        DeviceFamilyId.TAILWIND,
        "door-1",
        now=100.0,
        window_s=10.0,
    )
    assert is_expected_device_change(
        DeviceFamilyId.TAILWIND,
        "door-1",
        now=105.0,
    )
    assert not is_expected_device_change(
        DeviceFamilyId.TAILWIND,
        "door-1",
        now=111.0,
    )


def test_consume_expected_device_change_clears_mark() -> None:
    expected_device_changes.clear()
    mark_expected_device_change(
        DeviceFamilyId.SONOS,
        "RINCON_1",
        now=50.0,
        window_s=30.0,
    )
    assert consume_expected_device_change(
        DeviceFamilyId.SONOS,
        "RINCON_1",
        now=60.0,
    )
    assert not is_expected_device_change(
        DeviceFamilyId.SONOS,
        "RINCON_1",
        now=61.0,
    )
    assert not consume_expected_device_change(
        DeviceFamilyId.SONOS,
        "RINCON_1",
        now=61.0,
    )


def test_mark_ignores_blank_device_id() -> None:
    expected_device_changes.clear()
    mark_expected_device_change(DeviceFamilyId.KASA, "   ")
    assert not is_expected_device_change(DeviceFamilyId.KASA, "   ")


def test_resolve_expected_device_change_window_s_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S", raising=False)
    assert resolve_expected_device_change_window_s() == DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S


def test_resolve_expected_device_change_window_s_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S", "120.5")
    assert resolve_expected_device_change_window_s() == 120.5


def test_resolve_expected_device_change_window_s_rejects_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S", "nope")
    assert resolve_expected_device_change_window_s() == DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S
    monkeypatch.setenv("DOMESTI_EXPECTED_DEVICE_CHANGE_WINDOW_S", "0.5")
    assert resolve_expected_device_change_window_s() == DEFAULT_EXPECTED_DEVICE_CHANGE_WINDOW_S


@pytest.mark.asyncio
async def test_dispatch_device_action_marks_expected() -> None:
    expected_device_changes.clear()
    state = MagicMock()
    state.kasa_mgr = MagicMock()
    action = RuleDeviceActionOut(
        action=RuleDeviceActionType.TURN_OFF,
        device_id="Garage lights",
        family_id=DeviceFamilyId.KASA,
    )
    with patch(
        "app.rule_actions._dispatch_kasa_action",
        new_callable=AsyncMock,
    ) as dispatch:
        await dispatch_device_action(state, action)
        dispatch.assert_awaited_once()
    assert is_expected_device_change(DeviceFamilyId.KASA, "Garage lights")
    assert consume_expected_device_change(DeviceFamilyId.KASA, "Garage lights")


@pytest.mark.asyncio
async def test_flip_ui_device_marks_expected() -> None:
    expected_device_changes.clear()
    state = MagicMock()
    device = MagicMock()
    with (
        patch(
            "app.ui_device_actions._flip_tile",
            new_callable=AsyncMock,
            return_value=("Lamp", "on→off"),
        ),
        patch(
            "app.ui_device_actions._build_device_view",
            return_value=device,
        ),
    ):
        result = await flip_ui_device(
            state,
            family_id="kasa",
            device_id="192.168.1.10",
        )
    assert result.device is device
    assert is_expected_device_change(DeviceFamilyId.KASA, "192.168.1.10")
