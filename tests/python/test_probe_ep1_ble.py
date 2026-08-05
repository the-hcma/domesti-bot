"""Tests for the EP1 BLE probe command-line validation."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from collections.abc import Callable
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

import pytest


@pytest.mark.parametrize("duration", ["nan", "inf", "-inf"])
def test_probe_rejects_non_finite_duration(duration: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            Path(__file__).parents[2] / "scripts/internal/probe-ep1-ble",
            f"--duration={duration}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 2
    assert duration in result.stderr


def test_probe_prints_entities_and_redacted_advertisement(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_probe_module()
    monkeypatch.setattr(sys, "argv", ["probe-ep1-ble", "--host", "ep1.example", "--duration", "0.1"])
    monkeypatch.setattr(module, "APIClient", _FakeClient)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep_without_waiting)

    assert module.main() == 0

    output = capsys.readouterr().out
    assert "Connected to ep1.example:6053; 1 entities." in output
    assert "ble_presence" in output
    assert "address=redacted:" in output
    assert "Received 1 sampled advertisement records." in output
    assert _FakeClient.disconnect_forces == [True]


class _FakeClient:
    disconnect_forces: list[bool] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self._callback: object | None = None

    async def connect(self, *, login: bool) -> None:
        assert login is True

    async def disconnect(self, *, force: bool = False) -> None:
        self.disconnect_forces.append(force)

    async def list_entities_services(self) -> tuple[list[object], list[object]]:
        entity = type("Entity", (), {"object_id": "ble_presence", "name": "BLE presence"})()
        return [entity], []

    def subscribe_bluetooth_le_raw_advertisements(
        self,
        callback: Callable[[object], None],
    ) -> Callable[[], None]:
        self._callback = callback
        response = type(
            "Response",
            (),
            {
                "advertisements": [
                    type(
                        "Advertisement",
                        (),
                        {"address": 1, "address_type": "public", "data": b"\x01", "rssi": -50},
                    )()
                ]
            },
        )()
        callback(response)
        return lambda: None


def _load_probe_module() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts/internal/probe_ep1_ble.py"
    loader = SourceFileLoader("probe_ep1_ble_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


async def _sleep_without_waiting(_duration: float) -> None:
    """Replace the BLE sampling interval in the hermetic probe test."""
