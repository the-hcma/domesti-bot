"""Tests for :mod:`sonos_device_manager` (no LAN hardware)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from sonos_device_manager import SonosDeviceManager, SonosSpeakerDevice


@pytest.mark.asyncio
async def test_fetch_empty_discovery() -> None:
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("sonos_device_manager.soco_discover", return_value=set()):
        await mgr.fetch()
    assert mgr.players == ()


def test_transport_state_summary_maps_soco_states() -> None:
    zone = MagicMock()
    zone.uid = "u1"
    zone.player_name = "Office"
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PAUSED_PLAYBACK",
    }
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")
    assert dev.transport_state_summary() == "paused"

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }
    assert dev.transport_state_summary() == "playing"


@pytest.mark.asyncio
async def test_pause_resume_invokes_soco() -> None:
    zone = MagicMock()
    zone.uid = "RINCON_TEST12345678"
    zone.player_name = "Living room"

    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()

    await mgr.pause("Living room")
    zone.pause.assert_called_once()

    await mgr.resume("Living room")
    zone.play.assert_called_once()


@pytest.mark.asyncio
async def test_rediscover_refetches() -> None:
    z1 = MagicMock(uid="u1", player_name="A")
    z2 = MagicMock(uid="u2", player_name="B")
    mgr = SonosDeviceManager(discovery_timeout=0.1)

    with patch("sonos_device_manager.soco_discover", side_effect=[{z1}, {z2}]):
        await mgr.fetch()
        assert len(mgr.players) == 1
        await mgr.rediscover()
        assert len(mgr.players) == 1
        assert mgr.players[0].identifier == "u2"
