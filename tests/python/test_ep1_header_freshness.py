"""EP1 header freshness constants and liveness helpers (#574)."""

from __future__ import annotations

from pathlib import Path

from app.ep1_device_manager import Ep1Device
from app.ep1_header_freshness import (
    EP1_HEADER_EXPECTED_REFRESH_PERIOD_S,
    EP1_HEADER_STALE_AFTER_S,
    ep1_is_responding,
)


def test_apply_entity_state_also_bumps_last_heard() -> None:
    device = Ep1Device(
        "aa:bb:cc:dd:ee:ff",
        host="192.0.2.10",
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    device.apply_entity_state(temperature_c=21.0, updated_at=1_700_000_100.0)
    assert device.readings_updated_at == 1_700_000_100.0
    assert device.last_heard_at == 1_700_000_100.0


def test_ep1_header_stale_after_is_three_expected_periods() -> None:
    assert EP1_HEADER_EXPECTED_REFRESH_PERIOD_S == 5.0
    assert EP1_HEADER_STALE_AFTER_S == 3 * EP1_HEADER_EXPECTED_REFRESH_PERIOD_S
    assert EP1_HEADER_STALE_AFTER_S == 15.0


def test_ep1_is_responding_window() -> None:
    now = 1_700_000_000.0
    assert ep1_is_responding(None, now=now) is False
    assert ep1_is_responding(now, now=now) is True
    assert ep1_is_responding(now - EP1_HEADER_STALE_AFTER_S, now=now) is True
    assert ep1_is_responding(now - EP1_HEADER_STALE_AFTER_S - 0.001, now=now) is False


def test_note_heard_does_not_require_reading_change() -> None:
    device = Ep1Device(
        "aa:bb:cc:dd:ee:ff",
        host="192.0.2.10",
        mac_address="aa:bb:cc:dd:ee:ff",
    )
    assert device.last_heard_at is None
    assert device.readings_updated_at is None
    device.note_heard(at=1_700_000_000.0)
    assert device.last_heard_at == 1_700_000_000.0
    assert device.readings_updated_at is None


def test_typescript_mirrors_python_freshness_constants() -> None:
    src = _ep1_header_status_ts().read_text(encoding="utf-8")
    assert f"export const EP1_HEADER_EXPECTED_REFRESH_PERIOD_S = {int(EP1_HEADER_EXPECTED_REFRESH_PERIOD_S)};" in src
    assert "3 * EP1_HEADER_EXPECTED_REFRESH_PERIOD_S" in src
    assert "EP1_HEADER_OCCUPANCY_ARIA_OCCUPIED" in src
    assert "EP1_HEADER_OCCUPANCY_ARIA_CLEAR" in src
    assert "ep1HeaderOccupancyGlyphFromUiState" in src
    assert "createEp1HeaderOccupancyGlyph" in src
    assert "export const Ep1HeaderOccupancyGlyph" in src


def _ep1_header_status_ts() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "src" / "ep1-header-status.ts"
