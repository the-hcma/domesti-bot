"""Tests for :mod:`sonos_device_manager` (no LAN hardware)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import kasa_discovery_store
from app.sonos_device_manager import SonosDeviceManager, SonosSpeakerDevice


@pytest.mark.asyncio
async def test_fetch_empty_discovery() -> None:
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("app.sonos_device_manager.soco_discover", return_value=set()):
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
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
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

    with patch("app.sonos_device_manager.soco_discover", side_effect=[{z1}, {z2}]):
        await mgr.fetch()
        assert len(mgr.players) == 1
        await mgr.rediscover()
        assert len(mgr.players) == 1
        assert mgr.players[0].identifier == "u2"


@pytest.mark.asyncio
async def test_fetch_skips_udp_when_cache_warm(tmp_path: Path) -> None:
    """A populated cache must short-circuit ``soco_discover``."""

    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_AAA", "192.168.1.10", "Living Room"),
            ("RINCON_BBB", "192.168.1.11", "Kitchen"),
        ],
    )

    fakes: dict[str, MagicMock] = {}

    def _make_soco(host: str) -> MagicMock:
        z = MagicMock()
        z.ip_address = host
        if host == "192.168.1.10":
            z.uid = "RINCON_AAA"
            z.player_name = "Living Room"
        else:
            z.uid = "RINCON_BBB"
            z.player_name = "Kitchen"
        fakes[host] = z
        return z

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    assert mgr.is_cache_warm is True

    with (
        patch("app.sonos_device_manager.SoCo", side_effect=_make_soco) as soco_cls,
        patch("app.sonos_device_manager.soco_discover") as discover,
    ):
        await mgr.fetch()

    discover.assert_not_called()
    assert soco_cls.call_count == 2
    ids = sorted(p.identifier for p in mgr.players)
    assert ids == ["RINCON_AAA", "RINCON_BBB"]
    assert mgr.last_discovery_source == "cache"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_udp_when_cached_uid_changes(tmp_path: Path) -> None:
    """If a cached host now reports a different UID, the manager must re-probe via UDP."""

    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [("RINCON_OLD", "192.168.1.10", "Living Room")],
    )

    stale = MagicMock()
    stale.ip_address = "192.168.1.10"
    stale.uid = "RINCON_NEW"  # cache mismatch — host now hosts a different zone
    stale.player_name = "Living Room"

    fresh = MagicMock()
    fresh.ip_address = "192.168.1.10"
    fresh.uid = "RINCON_NEW"
    fresh.player_name = "Living Room"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)

    with (
        patch("app.sonos_device_manager.SoCo", return_value=stale),
        patch("app.sonos_device_manager.soco_discover", return_value={fresh}) as discover,
    ):
        await mgr.fetch()

    discover.assert_called_once()
    assert [p.identifier for p in mgr.players] == ["RINCON_NEW"]
    # And the cache must be refreshed to the new UID.
    assert kasa_discovery_store.load_sonos_zones(db) == [
        ("RINCON_NEW", "192.168.1.10", "Living Room"),
    ]
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_fetch_falls_back_when_cached_host_unreachable(tmp_path: Path) -> None:
    """A SoCo probe that raises must trigger UDP fallback rather than crashing."""

    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAA", "10.0.0.99", "Office")],
    )

    discovered = MagicMock()
    discovered.ip_address = "10.0.0.55"
    discovered.uid = "RINCON_AAA"
    discovered.player_name = "Office"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)

    def _raise(host: str) -> MagicMock:
        raise ConnectionError(f"unreachable: {host}")

    with (
        patch("app.sonos_device_manager.SoCo", side_effect=_raise),
        patch(
            "app.sonos_device_manager.soco_discover", return_value={discovered}
        ) as discover,
    ):
        await mgr.fetch()

    discover.assert_called_once()
    assert [p.identifier for p in mgr.players] == ["RINCON_AAA"]
    # Cache rewritten with the new host the zone is at.
    assert kasa_discovery_store.load_sonos_zones(db) == [
        ("RINCON_AAA", "10.0.0.55", "Office"),
    ]


@pytest.mark.asyncio
async def test_force_discovery_always_runs_udp(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAA", "192.168.1.10", "Living Room")],
    )

    zone = MagicMock()
    zone.uid = "RINCON_AAA"
    zone.player_name = "Living Room"
    zone.ip_address = "192.168.1.10"

    mgr = SonosDeviceManager(
        discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True
    )
    assert mgr.is_cache_warm is False

    with (
        patch("app.sonos_device_manager.SoCo") as soco_cls,
        patch("app.sonos_device_manager.soco_discover", return_value={zone}) as discover,
    ):
        await mgr.fetch()

    soco_cls.assert_not_called()
    discover.assert_called_once()
    assert [p.identifier for p in mgr.players] == ["RINCON_AAA"]
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_fetch_persists_cache_after_udp_discovery(tmp_path: Path) -> None:
    """A successful UDP sweep must write the cache so the next start can skip it."""

    db = tmp_path / "sonos.sqlite"
    zone = MagicMock()
    zone.uid = "RINCON_ZZZ"
    zone.ip_address = "192.168.1.42"
    zone.player_name = "Den"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    assert mgr.is_cache_warm is False  # empty cache file
    assert mgr.last_discovery_source is None

    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()

    assert kasa_discovery_store.load_sonos_zones(db) == [
        ("RINCON_ZZZ", "192.168.1.42", "Den"),
    ]
    assert mgr.last_discovery_source == "discovery"


def test_is_cache_warm_false_without_cache_path() -> None:
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    assert mgr.is_cache_warm is False


def test_is_cache_warm_false_when_cache_file_missing(tmp_path: Path) -> None:
    mgr = SonosDeviceManager(
        discovery_timeout=0.1, discovery_cache_path=tmp_path / "absent.sqlite"
    )
    assert mgr.is_cache_warm is False
