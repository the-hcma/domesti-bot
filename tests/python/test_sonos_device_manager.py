"""Tests for :mod:`sonos_device_manager` (no LAN hardware)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from soco.exceptions import SoCoUPnPException

from app import kasa_discovery_store
from app.sonos_device_manager import (
    SonosDeviceManager,
    SonosSpeakerDevice,
    SonosTransitionUnavailableError,
)


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
    zone.play_uri.assert_not_called()


@pytest.mark.asyncio
async def test_resume_uses_play_uri_when_stream_favorites_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "domesti-secrets.json"
    secrets.write_text(
        json.dumps(
            {
                "sonos_stream_favorites": {
                    "Living room": [
                        {
                            "name": "Alvorada FM",
                            "uri": "https://example.com/stream.aac",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_SECRETS_FILE", str(secrets))

    zone = MagicMock()
    zone.uid = "RINCON_TEST12345678"
    zone.player_name = "Living room"

    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()

    await mgr.resume("Living room", favorite_index=0)
    zone.play_uri.assert_called_once_with(
        "https://example.com/stream.aac",
        title="Alvorada FM",
        force_radio=True,
    )
    zone.play.assert_not_called()


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
async def test_fetch_cache_hit_uses_live_zone_name_over_stale_cache_label(
    tmp_path: Path,
) -> None:
    """Renamed Sonos zones must show the current ``player_name``, not SQLite ``zone_name``."""

    db = tmp_path / "sonos.sqlite"
    kasa_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAA", "192.168.1.10", "Old Kitchen Name")],
    )

    zone = MagicMock()
    zone.ip_address = "192.168.1.10"
    zone.uid = "RINCON_AAA"
    zone.player_name = "Kitchen"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    with (
        patch("app.sonos_device_manager.SoCo", return_value=zone),
        patch("app.sonos_device_manager.soco_discover") as discover,
    ):
        await mgr.fetch()

    discover.assert_not_called()
    assert mgr.players[0].preferred_label == "Kitchen"
    assert kasa_discovery_store.load_sonos_zones(db) == [
        ("RINCON_AAA", "192.168.1.10", "Kitchen"),
    ]


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


def test_is_playing_cache_starts_none() -> None:
    """A freshly constructed zone hasn't been polled yet — the UI tile
    renders ``state="unknown"`` until the watcher (or an action) fills
    in a definite value. Asserts the contract :func:`_sonos_state`
    relies on."""

    zone = MagicMock(uid="u1", player_name="X")
    dev = SonosSpeakerDevice("u1", zone, display_name="X")
    assert dev.is_playing is None


@pytest.mark.asyncio
async def test_pause_resume_sync_is_playing_cache() -> None:
    """``pause`` / ``resume`` must trust the commanded state, not race
    a re-poll. Sonos lingers in ``TRANSITIONING`` for a beat after
    every transport call, so reading the cache after the action would
    flicker the UI tile back to ``unknown``. The post-action sync is
    what keeps the optimistic UI update honest."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")
    assert dev.is_playing is None
    await dev.resume()
    assert dev.is_playing is True
    await dev.pause()
    assert dev.is_playing is False


def test_transport_state_summary_updates_is_playing_cache() -> None:
    """``transport_state_summary`` is the live UPnP path used by the
    watcher; the cache it leaves behind drives the cheap UI render."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }
    assert dev.transport_state_summary() == "playing"
    assert dev.is_playing is True

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PAUSED_PLAYBACK",
    }
    assert dev.transport_state_summary() == "paused"
    assert dev.is_playing is False

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "STOPPED",
    }
    assert dev.transport_state_summary() == "stopped"
    # ``stopped`` is mapped to "not playing" so a stopped zone shows
    # as ``paused`` in the UI rather than getting stuck on
    # ``unknown`` forever.
    assert dev.is_playing is False


def test_transport_state_summary_leaves_cache_intact_on_failure() -> None:
    """A flaky UPnP read must not flicker the tile — keep the last
    known value so transient LAN blips don't churn the state badge."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }
    dev.transport_state_summary()
    assert dev.is_playing is True
    zone.get_current_transport_info.side_effect = RuntimeError("upnp boom")
    assert dev.transport_state_summary() == "unknown"
    assert dev.is_playing is True


def _upnp_error(code: str, message: str) -> SoCoUPnPException:
    """Build a SoCoUPnPException matching the real one's positional args."""

    return SoCoUPnPException(message=message, error_code=code, error_xml="")


@pytest.mark.asyncio
async def test_pause_raises_domain_error_on_upnp_701() -> None:
    """A pause that hits UPnP 701 must surface as the domain error so
    the HTTP layer can return 409 instead of leaking an opaque 500.
    Other UPnP faults must keep propagating — only 701 is the
    "transition not possible" case we know how to translate."""

    zone = MagicMock(uid="u1", player_name="Office")
    zone.pause.side_effect = _upnp_error("701", "Transition not available")
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "STOPPED",
    }
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")

    with pytest.raises(SonosTransitionUnavailableError) as exc_info:
        await dev.pause()
    assert isinstance(exc_info.value.__cause__, SoCoUPnPException)
    # The handler refreshes from a live UPnP read so the cache mirrors
    # truth (the zone is stopped → not playing) before returning to
    # the caller. Without this, the optimistic UI flip would leave a
    # stale ``is_playing=True`` value in place.
    assert dev.is_playing is False


@pytest.mark.asyncio
async def test_pause_propagates_non_701_upnp_errors() -> None:
    zone = MagicMock(uid="u1", player_name="Office")
    zone.pause.side_effect = _upnp_error("500", "Internal server error")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")

    with pytest.raises(SoCoUPnPException):
        await dev.pause()


@pytest.mark.asyncio
async def test_resume_raises_domain_error_on_upnp_701_empty_queue() -> None:
    """The original 500 the user reported. UPnP 701 on play() means
    the zone has nothing to play (empty queue or mid-transition);
    callers see a clean domain exception they can map to 409."""

    zone = MagicMock(uid="u1", player_name="Living Room")
    zone.play.side_effect = _upnp_error("701", "Transition not available")
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "STOPPED",
    }
    dev = SonosSpeakerDevice("u1", zone, display_name="Living Room")

    with pytest.raises(SonosTransitionUnavailableError) as exc_info:
        await dev.resume()
    assert "Living Room" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, SoCoUPnPException)
    # Cache reflects reality after the failed action — the zone is
    # stopped, not playing.
    assert dev.is_playing is False


@pytest.mark.asyncio
async def test_resume_propagates_non_701_upnp_errors() -> None:
    zone = MagicMock(uid="u1", player_name="Office")
    zone.play.side_effect = _upnp_error("500", "Internal server error")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office")

    with pytest.raises(SoCoUPnPException):
        await dev.resume()


@pytest.mark.asyncio
async def test_manager_is_playing_refreshes_cache() -> None:
    """The polling watcher drives refresh through this method —
    asserts it forces a fresh transport read on each call and
    returns the cached flag."""

    zone = MagicMock(uid="RINCON_TEST", player_name="Living room")
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }

    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()

    assert await mgr.is_playing("Living room") is True
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PAUSED_PLAYBACK",
    }
    assert await mgr.is_playing("Living room") is False
