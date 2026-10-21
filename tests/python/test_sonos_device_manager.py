"""Tests for :mod:`sonos_device_manager` (no LAN hardware)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from soco.exceptions import SoCoUPnPException

from app import device_discovery_store
from app.device_presence import DevicePresenceKind, DevicePresenceStore
from app.sonos_device_manager import (
    UNRESPONSIVE_AFTER_FAILED_POLLS,
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
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")
    assert dev.transport_state_summary() == "paused"

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PLAYING",
    }
    assert dev.transport_state_summary() == "playing"


@pytest.mark.asyncio
async def test_pause_resume_invokes_soco(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing-config.json"))
    zone = MagicMock()
    zone.uid = "RINCON_AA112233445501400"
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
    secrets = tmp_path / "domesti-bot.config.json"
    secrets.write_text(
        json.dumps(
            {
                "sonos_stream_favorites": [
                    {
                        "name": "Alvorada FM",
                        "uri": "https://example.com/stream.aac",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(secrets))

    zone = MagicMock()
    zone.uid = "RINCON_AA112233445501400"
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
    z1 = MagicMock(uid="RINCON_AAAAAAAAAAAA01400", player_name="A")
    z2 = MagicMock(uid="RINCON_BBBBBBBBBBBB01400", player_name="B")
    mgr = SonosDeviceManager(discovery_timeout=0.1)

    with patch("app.sonos_device_manager.soco_discover", side_effect=[{z1}, {z2}]):
        await mgr.fetch()
        assert len(mgr.players) == 1
        await mgr.rediscover()
        assert len(mgr.players) == 1
        assert mgr.players[0].identifier == "bb:bb:bb:bb:bb:bb"


@pytest.mark.asyncio
async def test_fetch_skips_udp_when_cache_warm(tmp_path: Path) -> None:
    """A populated cache must short-circuit ``soco_discover``."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [
            ("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Living Room"),
            ("RINCON_BBBBBBBBBBBB01400", "192.168.1.11", "Kitchen"),
        ],
    )

    fakes: dict[str, MagicMock] = {}

    def _make_soco(host: str) -> MagicMock:
        z = MagicMock()
        z.ip_address = host
        if host == "192.168.1.10":
            z.uid = "RINCON_AAAAAAAAAAAA01400"
            z.player_name = "Living Room"
        else:
            z.uid = "RINCON_BBBBBBBBBBBB01400"
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
    assert ids == ["aa:aa:aa:aa:aa:aa", "bb:bb:bb:bb:bb:bb"]
    assert mgr.last_discovery_source == "cache"


@pytest.mark.asyncio
async def test_fetch_cache_hit_uses_live_zone_name_over_stale_cache_label(
    tmp_path: Path,
) -> None:
    """Renamed Sonos speakers must show the current ``player_name``, not SQLite ``zone_name``."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Old Kitchen Name")],
    )

    zone = MagicMock()
    zone.ip_address = "192.168.1.10"
    zone.uid = "RINCON_AAAAAAAAAAAA01400"
    zone.player_name = "Kitchen"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    with (
        patch("app.sonos_device_manager.SoCo", return_value=zone),
        patch("app.sonos_device_manager.soco_discover") as discover,
    ):
        await mgr.fetch()

    discover.assert_not_called()
    assert mgr.players[0].preferred_label == "Kitchen"
    assert device_discovery_store.load_sonos_zones(db) == [
        ("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Kitchen", "aa:aa:aa:aa:aa:aa"),
    ]


@pytest.mark.asyncio
async def test_fetch_falls_back_to_udp_when_cached_uid_changes(tmp_path: Path) -> None:
    """If a cached host now reports a different UID, the manager must re-probe via UDP."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_DDDDDDDDDDDD01400", "192.168.1.10", "Living Room")],
    )

    stale = MagicMock()
    stale.ip_address = "192.168.1.10"
    stale.uid = "RINCON_EEEEEEEEEEEE01400"  # cache mismatch — host now hosts a different zone
    stale.player_name = "Living Room"

    fresh = MagicMock()
    fresh.ip_address = "192.168.1.10"
    fresh.uid = "RINCON_EEEEEEEEEEEE01400"
    fresh.player_name = "Living Room"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)

    with (
        patch("app.sonos_device_manager.SoCo", return_value=stale),
        patch("app.sonos_device_manager.soco_discover", return_value={fresh}) as discover,
    ):
        await mgr.fetch()

    discover.assert_called_once()
    # The new speaker is live. The old one (cached uid DDDD, no longer answering anywhere) is not
    # dropped: it stays as an unresponsive tile until the operator forgets it (#876).
    by_mac = {p.mac_address: p for p in mgr.players}
    assert set(by_mac) == {"ee:ee:ee:ee:ee:ee", "dd:dd:dd:dd:dd:dd"}
    assert by_mac["ee:ee:ee:ee:ee:ee"].unresponsive is False
    assert by_mac["dd:dd:dd:dd:dd:dd"].unresponsive is True
    assert sorted(device_discovery_store.load_sonos_zones(db)) == [
        ("RINCON_DDDDDDDDDDDD01400", "192.168.1.10", "Living Room", "dd:dd:dd:dd:dd:dd"),
        ("RINCON_EEEEEEEEEEEE01400", "192.168.1.10", "Living Room", "ee:ee:ee:ee:ee:ee"),
    ]
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_fetch_falls_back_when_cached_host_unreachable(tmp_path: Path) -> None:
    """A SoCo probe that raises must trigger UDP fallback rather than crashing."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAAAAAAAAAAA01400", "10.0.0.99", "Office")],
    )

    discovered = MagicMock()
    discovered.ip_address = "10.0.0.55"
    discovered.uid = "RINCON_AAAAAAAAAAAA01400"
    discovered.player_name = "Office"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)

    def _raise(host: str) -> MagicMock:
        raise ConnectionError(f"unreachable: {host}")

    with (
        patch("app.sonos_device_manager.SoCo", side_effect=_raise),
        patch("app.sonos_device_manager.soco_discover", return_value={discovered}) as discover,
    ):
        await mgr.fetch()

    discover.assert_called_once()
    assert [p.identifier for p in mgr.players] == ["aa:aa:aa:aa:aa:aa"]
    # Cache rewritten with the new host the speaker is at.
    assert device_discovery_store.load_sonos_zones(db) == [
        ("RINCON_AAAAAAAAAAAA01400", "10.0.0.55", "Office", "aa:aa:aa:aa:aa:aa"),
    ]


@pytest.mark.asyncio
async def test_force_discovery_always_runs_udp(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Living Room")],
    )

    zone = MagicMock()
    zone.uid = "RINCON_AAAAAAAAAAAA01400"
    zone.player_name = "Living Room"
    zone.ip_address = "192.168.1.10"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True)
    assert mgr.is_cache_warm is False

    with (
        patch("app.sonos_device_manager.SoCo") as soco_cls,
        patch("app.sonos_device_manager.soco_discover", return_value={zone}) as discover,
    ):
        await mgr.fetch()

    soco_cls.assert_not_called()
    discover.assert_called_once()
    assert [p.identifier for p in mgr.players] == ["aa:aa:aa:aa:aa:aa"]
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_force_discovery_keeps_arp_visible_cached_zone_as_unresponsive(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(
        db,
        [("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Living Room", "aa:aa:aa:aa:aa:aa")],
    )
    zone = MagicMock()
    zone.uid = "RINCON_AAAAAAAAAAAA01400"
    zone.player_name = "Living Room"
    zone.ip_address = "192.168.1.10"
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True)
    with (
        patch("app.sonos_device_manager.soco_discover", return_value=set()),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=True),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.10"),
        patch("app.sonos_device_manager.SoCo", return_value=zone),
    ):
        await mgr.fetch()
    assert len(mgr.players) == 1
    player = mgr.players[0]
    assert player.mac_address == "aa:aa:aa:aa:aa:aa"
    assert player.unresponsive is True


def _zone(uid: str, host: str, name: str) -> MagicMock:
    zone = MagicMock()
    zone.uid = uid
    zone.ip_address = host
    zone.player_name = name
    return zone


_LIVING_ROOM = ("RINCON_AAAAAAAAAAAA01400", "192.168.1.10", "Living Room", "aa:aa:aa:aa:aa:aa")
_KITCHEN = ("RINCON_BBBBBBBBBBBB01400", "192.168.1.20", "Kitchen", "bb:bb:bb:bb:bb:bb")


@pytest.mark.asyncio
async def test_startup_sweep_keeps_an_unplugged_cached_speaker_as_unresponsive(tmp_path: Path) -> None:
    """A speaker that answers neither UPnP nor ARP keeps its tile and cache row on a normal startup (#876)."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(db, [_LIVING_ROOM, _KITCHEN])
    living_room = _zone(_LIVING_ROOM[0], _LIVING_ROOM[1], "Living Room")

    def _soco(host: str) -> MagicMock:
        if host == _KITCHEN[1]:
            # soco.SoCo(ip) never touches the network when built; reading .uid does.
            unreachable = MagicMock()
            type(unreachable).uid = PropertyMock(side_effect=ConnectionError(f"unreachable: {host}"))
            unreachable.ip_address = host
            unreachable.player_name = "Kitchen"
            return unreachable
        return living_room

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    with (
        patch("app.sonos_device_manager.SoCo", side_effect=_soco),
        patch("app.sonos_device_manager.soco_discover", return_value={living_room}) as discover,
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
    ):
        await mgr.fetch()
    discover.assert_called_once()
    assert mgr.last_discovery_source == "discovery"
    by_mac = {p.mac_address: p for p in mgr.players}
    assert set(by_mac) == {_LIVING_ROOM[3], _KITCHEN[3]}
    assert by_mac[_LIVING_ROOM[3]].unresponsive is False
    assert by_mac[_KITCHEN[3]].unresponsive is True
    assert by_mac[_KITCHEN[3]].preferred_label == "Kitchen"
    assert sorted(device_discovery_store.load_sonos_zones(db)) == sorted([_LIVING_ROOM, _KITCHEN])


@pytest.mark.asyncio
async def test_forced_discovery_keeps_an_unplugged_cached_speaker_as_unresponsive(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(db, [_KITCHEN])
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True)
    with (
        patch("app.sonos_device_manager.soco_discover", return_value=set()),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.sonos_device_manager.SoCo", return_value=_zone(_KITCHEN[0], _KITCHEN[1], "Kitchen")),
    ):
        await mgr.fetch()
    assert [(p.mac_address, p.unresponsive) for p in mgr.players] == [(_KITCHEN[3], True)]
    assert device_discovery_store.load_sonos_zones(db) == [_KITCHEN]


@pytest.mark.asyncio
async def test_rediscover_keeps_an_unplugged_cached_speaker_after_it_was_live(tmp_path: Path) -> None:
    """The speaker was live at startup, then lost power; the next rediscover must not drop it."""

    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(db, [_LIVING_ROOM])
    living_room = _zone(_LIVING_ROOM[0], _LIVING_ROOM[1], "Living Room")
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    with patch("app.sonos_device_manager.SoCo", return_value=living_room):
        await mgr.fetch()
    assert [p.unresponsive for p in mgr.players] == [False]
    with (
        patch("app.sonos_device_manager.soco_discover", return_value=set()),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.sonos_device_manager.SoCo", return_value=living_room),
    ):
        await mgr.rediscover()
    assert [(p.mac_address, p.unresponsive) for p in mgr.players] == [(_LIVING_ROOM[3], True)]
    assert device_discovery_store.load_sonos_zones(db) == [_LIVING_ROOM]


@pytest.mark.asyncio
async def test_retained_speaker_keeps_its_cached_mac_when_another_speaker_took_its_address(tmp_path: Path) -> None:
    """A row whose id carries no MAC must not adopt the MAC of whatever now answers at its old address."""

    db = tmp_path / "sonos.sqlite"
    legacy = ("LEGACY-UID-1", "192.168.1.10", "Living Room", "dd:dd:dd:dd:dd:dd")
    device_discovery_store.save_sonos_zones(db, [legacy])
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True)
    with (
        patch("app.sonos_device_manager.soco_discover", return_value=set()),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        # Another speaker now answers ARP at the old address.
        patch("app.sonos_device_manager.lookup_mac_via_arp", return_value="ee:ee:ee:ee:ee:ee"),
        patch("app.sonos_device_manager.SoCo", return_value=_zone(legacy[0], legacy[1], "Living Room")),
    ):
        await mgr.fetch()
    assert [(p.mac_address, p.unresponsive) for p in mgr.players] == [("dd:dd:dd:dd:dd:dd", True)]
    assert device_discovery_store.load_sonos_zones(db) == [legacy]


@pytest.mark.asyncio
async def test_a_shared_room_name_resolves_to_the_live_speaker_not_the_retained_one(tmp_path: Path) -> None:
    """A replaced speaker usually keeps its room name: pausing "Living Room" must reach the live one."""

    db = tmp_path / "sonos.sqlite"
    old = ("RINCON_DDDDDDDDDDDD01400", "192.168.1.10", "Living Room", "dd:dd:dd:dd:dd:dd")
    device_discovery_store.save_sonos_zones(db, [old])
    replacement = _zone("RINCON_EEEEEEEEEEEE01400", "192.168.1.11", "Living Room")
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True)
    with (
        patch("app.sonos_device_manager.soco_discover", return_value={replacement}),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=False),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.sonos_device_manager.SoCo", return_value=_zone(old[0], old[1], "Living Room")),
    ):
        await mgr.fetch()
    by_label = mgr.get_device_by_alias("Living Room")
    assert by_label is not None
    assert (by_label.mac_address, by_label.unresponsive) == ("ee:ee:ee:ee:ee:ee", False)
    # Both speakers stay reachable by their own MAC.
    retained = mgr.get_device_by_alias("dd:dd:dd:dd:dd:dd")
    assert retained is not None and retained.unresponsive is True


@pytest.mark.asyncio
async def test_fetch_persists_cache_after_udp_discovery(tmp_path: Path) -> None:
    """A successful UDP sweep must write the cache so the next start can skip it."""

    db = tmp_path / "sonos.sqlite"
    zone = MagicMock()
    zone.uid = "RINCON_CCCCCCCCCCCC01400"
    zone.ip_address = "192.168.1.42"
    zone.player_name = "Den"

    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db)
    assert mgr.is_cache_warm is False  # empty cache file
    assert mgr.last_discovery_source is None

    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()

    assert device_discovery_store.load_sonos_zones(db) == [
        ("RINCON_CCCCCCCCCCCC01400", "192.168.1.42", "Den", "cc:cc:cc:cc:cc:cc"),
    ]
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_fetch_primes_is_playing_cache() -> None:
    """After discovery, speakers should have a definite playback flag so the
    first UI poll is not stuck on yellow ``unknown``."""

    zone = MagicMock(
        uid="RINCON_AA112233445501400",
        player_name="Living room",
        ip_address="192.168.1.10",
    )
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "PAUSED_PLAYBACK",
    }
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()
    assert mgr.players[0].is_playing is False
    zone.get_current_transport_info.assert_called()


def test_is_cache_warm_false_without_cache_path() -> None:
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    assert mgr.is_cache_warm is False


def test_is_cache_warm_false_when_cache_file_missing(tmp_path: Path) -> None:
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=tmp_path / "absent.sqlite")
    assert mgr.is_cache_warm is False


def test_host_is_cached_at_construction() -> None:
    """``host`` must not touch ``SoCo.ip_address`` after construction.

    Concurrent UI polls reading ``ip_address`` while the watcher runs
    UPnP on the same SoCo have left ``is_playing`` stuck at ``None`` in
    production (yellow tiles).
    """

    zone = MagicMock(uid="u1", player_name="Office", ip_address="192.168.1.50")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")
    assert dev.host == "192.168.1.50"
    zone.ip_address = "10.0.0.1"
    assert dev.host == "192.168.1.50"


def test_is_playing_cache_starts_none() -> None:
    """A freshly constructed zone hasn't been polled yet — the UI tile
    renders ``state="unknown"`` until the watcher (or an action) fills
    in a definite value. Asserts the contract :func:`_sonos_state`
    relies on."""

    zone = MagicMock(uid="u1", player_name="X")
    dev = SonosSpeakerDevice("u1", zone, display_name="X", mac_address="aa:bb:cc:dd:ee:ff")
    assert dev.is_playing is None


@pytest.mark.asyncio
async def test_pause_resume_sync_is_playing_cache() -> None:
    """``pause`` / ``resume`` must trust the commanded state, not race
    a re-poll. Sonos lingers in ``TRANSITIONING`` for a beat after
    every transport call, so reading the cache after the action would
    flicker the UI tile back to ``unknown``. The post-action sync is
    what keeps the optimistic UI update honest."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")
    assert dev.is_playing is None
    await dev.resume()
    assert dev.is_playing is True
    await dev.pause()
    assert dev.is_playing is False


def test_transport_state_summary_updates_is_playing_cache() -> None:
    """``transport_state_summary`` is the live UPnP path used by the
    watcher; the cache it leaves behind drives the cheap UI render."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")

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

    zone.get_current_transport_info.return_value = {
        "current_transport_state": "TRANSITIONING",
    }
    assert dev.transport_state_summary() == "transitioning"
    # Mid-transition must not leave the cache at ``None`` (yellow
    # ``unknown`` tile) after a successful UPnP read.
    assert dev.is_playing is False


def test_transport_state_summary_leaves_cache_intact_on_failure() -> None:
    """A flaky UPnP read must not flicker the tile — keep the last
    known value so transient LAN blips don't churn the state badge."""

    zone = MagicMock(uid="u1", player_name="Office")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")
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
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")

    with pytest.raises(SonosTransitionUnavailableError) as exc_info:
        await dev.pause()
    assert isinstance(exc_info.value.__cause__, SoCoUPnPException)
    # The handler refreshes from a live UPnP read so the cache mirrors
    # truth (the speaker is stopped → not playing) before returning to
    # the caller. Without this, the optimistic UI flip would leave a
    # stale ``is_playing=True`` value in place.
    assert dev.is_playing is False


@pytest.mark.asyncio
async def test_pause_propagates_non_701_upnp_errors() -> None:
    zone = MagicMock(uid="u1", player_name="Office")
    zone.pause.side_effect = _upnp_error("500", "Internal server error")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")

    with pytest.raises(SoCoUPnPException):
        await dev.pause()


@pytest.mark.asyncio
async def test_resume_raises_domain_error_on_upnp_701_empty_queue() -> None:
    """The original 500 the user reported. UPnP 701 on play() means
    the speaker has nothing to play (empty queue or mid-transition);
    callers see a clean domain exception they can map to 409."""

    zone = MagicMock(uid="u1", player_name="Living Room")
    zone.play.side_effect = _upnp_error("701", "Transition not available")
    zone.get_current_transport_info.return_value = {
        "current_transport_state": "STOPPED",
    }
    dev = SonosSpeakerDevice("u1", zone, display_name="Living Room", mac_address="aa:bb:cc:dd:ee:ff")

    with pytest.raises(SonosTransitionUnavailableError) as exc_info:
        await dev.resume()
    assert "Living Room" in str(exc_info.value)
    assert "nothing to resume" in str(exc_info.value).lower()
    assert isinstance(exc_info.value.__cause__, SoCoUPnPException)
    # Cache reflects reality after the failed action — the speaker is
    # stopped, not playing.
    assert dev.is_playing is False


@pytest.mark.asyncio
async def test_resume_propagates_non_701_upnp_errors() -> None:
    zone = MagicMock(uid="u1", player_name="Office")
    zone.play.side_effect = _upnp_error("500", "Internal server error")
    dev = SonosSpeakerDevice("u1", zone, display_name="Office", mac_address="aa:bb:cc:dd:ee:ff")

    with pytest.raises(SoCoUPnPException):
        await dev.resume()


@pytest.mark.asyncio
async def test_manager_is_playing_refreshes_cache() -> None:
    """The polling watcher drives refresh through this method —
    asserts it forces a fresh transport read on each call and
    returns the cached flag."""

    zone = MagicMock(uid="RINCON_AA112233445501400", player_name="Living room")
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


def test_speaker_device_uses_mac_from_rincon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.sonos_device_manager.lookup_mac_via_arp", lambda host: None)
    zone = MagicMock()
    zone.ip_address = "192.168.1.10"
    zone.uid = "RINCON_5CAAFD0A123401400"
    zone.player_name = "Living Room"
    mgr = SonosDeviceManager(discovery_timeout=0.1)
    sd = mgr._speaker_device(zone.uid, zone, display_name="Living Room")
    assert sd is not None
    assert sd.mac_address == "5c:aa:fd:0a:12:34"
    assert sd.identifier == "5c:aa:fd:0a:12:34"
    assert sd.rincon_uid == "RINCON_5CAAFD0A123401400"


async def _polled_manager(store: DevicePresenceStore) -> tuple[SonosDeviceManager, MagicMock]:
    zone = MagicMock(uid="RINCON_AA112233445501400", player_name="Living room", ip_address="192.168.1.10")
    zone.get_current_transport_info.return_value = {"current_transport_state": "PLAYING"}
    mgr = SonosDeviceManager(discovery_timeout=0.1, presence_store=store)
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.fetch()
    return mgr, zone


@pytest.mark.asyncio
async def test_speaker_is_flagged_after_consecutive_failed_polls_and_announced() -> None:
    store = DevicePresenceStore()
    mgr, zone = await _polled_manager(store)
    zone.get_current_transport_info.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        assert await mgr.is_playing("Living room") is True
    assert store.notices() == ()
    assert mgr.players[0].unresponsive is False
    await mgr.is_playing("Living room")
    assert mgr.players[0].unresponsive is True
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id, notice.label) == (
        DevicePresenceKind.DISAPPEARED,
        "aa:11:22:33:44:55",
        "Living room",
    )


@pytest.mark.asyncio
async def test_a_successful_speaker_poll_resets_the_failure_count() -> None:
    store = DevicePresenceStore()
    mgr, zone = await _polled_manager(store)
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        zone.get_current_transport_info.side_effect = TimeoutError("no answer")
        await mgr.is_playing("Living room")
    zone.get_current_transport_info.side_effect = None
    await mgr.is_playing("Living room")
    zone.get_current_transport_info.side_effect = TimeoutError("no answer")
    await mgr.is_playing("Living room")
    assert mgr.players[0].unresponsive is False
    assert store.notices() == ()


@pytest.mark.asyncio
async def test_flagged_speaker_that_answers_a_poll_again_is_announced_as_reappeared() -> None:
    store = DevicePresenceStore()
    mgr, zone = await _polled_manager(store)
    zone.get_current_transport_info.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS):
        await mgr.is_playing("Living room")
    zone.get_current_transport_info.side_effect = None
    await mgr.is_playing("Living room")
    assert mgr.players[0].unresponsive is False
    (notice,) = store.notices()
    assert notice.kind == DevicePresenceKind.REAPPEARED


@pytest.mark.asyncio
async def test_rediscover_resets_the_failed_poll_count_of_a_healthy_speaker() -> None:
    store = DevicePresenceStore()
    mgr, zone = await _polled_manager(store)
    zone.get_current_transport_info.side_effect = TimeoutError("no answer")
    for _ in range(UNRESPONSIVE_AFTER_FAILED_POLLS - 1):
        await mgr.is_playing("Living room")
    zone.get_current_transport_info.side_effect = None
    with patch("app.sonos_device_manager.soco_discover", return_value={zone}):
        await mgr.rediscover()
    zone.get_current_transport_info.side_effect = TimeoutError("no answer")
    await mgr.is_playing("Living room")
    assert mgr.players[0].unresponsive is False
    assert store.notices() == ()


_LIVING_ROOM_UID = "RINCON_AAAAAAAAAAAA01400"
_LIVING_ROOM_MAC = "aa:aa:aa:aa:aa:aa"


def _speaker_zone(uid: str, host: str, name: str = "Living Room") -> MagicMock:
    zone = MagicMock(uid=uid, player_name=name, ip_address=host)
    # The live device description names the speaker by its serial number, which carries the MAC.
    hex12 = uid.removeprefix("RINCON_")[:12]
    zone.get_speaker_info.return_value = {
        "mac_address": "-".join(hex12[i : i + 2] for i in range(0, 12, 2)).upper(),
        "zone_name": name,
    }
    zone.get_current_transport_info.return_value = {"current_transport_state": "PAUSED_PLAYBACK"}
    return zone


async def _manager_with_a_missing_speaker(
    tmp_path: Path, store: DevicePresenceStore
) -> tuple[SonosDeviceManager, Path]:
    """A manager whose only cached speaker answered nothing during the sweep, so it is a stub."""
    db = tmp_path / "sonos.sqlite"
    device_discovery_store.save_sonos_zones(db, [(_LIVING_ROOM_UID, "192.168.1.10", "Living Room", _LIVING_ROOM_MAC)])
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, force_discovery=True, presence_store=store)
    with (
        patch("app.sonos_device_manager.soco_discover", return_value=set()),
        patch("app.sonos_device_manager.mac_alive_on_lan", return_value=True),
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.10"),
        patch("app.sonos_device_manager.SoCo", return_value=_speaker_zone(_LIVING_ROOM_UID, "192.168.1.10")),
    ):
        await mgr.fetch()
    assert mgr.players[0].unresponsive is True
    return mgr, db


@pytest.mark.asyncio
async def test_recover_unresponsive_reconnects_a_speaker_that_moved_to_a_new_address(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr, db = await _manager_with_a_missing_speaker(tmp_path, store)
    assert store.notices() == ()
    moved = _speaker_zone(_LIVING_ROOM_UID, "192.168.1.20")
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.20"),
        patch("app.sonos_device_manager.SoCo", return_value=moved),
    ):
        assert await mgr.recover_unresponsive() == (_LIVING_ROOM_MAC,)
    (player,) = mgr.players
    assert (player.unresponsive, player.host, player.is_playing) == (False, "192.168.1.20", False)
    assert device_discovery_store.load_sonos_zones(db) == [
        (_LIVING_ROOM_UID, "192.168.1.20", "Living Room", _LIVING_ROOM_MAC)
    ]


@pytest.mark.asyncio
async def test_recover_unresponsive_announces_the_return(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, store)
    store.note_disappeared(device_id=_LIVING_ROOM_MAC, family_id="sonos", host="192.168.1.10", label="Living Room")
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.20"),
        patch("app.sonos_device_manager.SoCo", return_value=_speaker_zone(_LIVING_ROOM_UID, "192.168.1.20")),
    ):
        await mgr.recover_unresponsive()
    (notice,) = store.notices()
    assert (notice.kind, notice.device_id) == (DevicePresenceKind.REAPPEARED, _LIVING_ROOM_MAC)


@pytest.mark.asyncio
async def test_recover_unresponsive_leaves_a_silent_speaker_untouched(tmp_path: Path) -> None:
    mgr, db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    silent = MagicMock(ip_address="192.168.1.10")
    silent.get_speaker_info.side_effect = TimeoutError("no answer")
    before = device_discovery_store.load_sonos_zones(db)
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.10"),
        patch("app.sonos_device_manager.SoCo", return_value=silent),
    ):
        assert await mgr.recover_unresponsive() == ()
    assert mgr.players[0].unresponsive is True
    assert device_discovery_store.load_sonos_zones(db) == before


@pytest.mark.asyncio
async def test_recover_unresponsive_ignores_another_speaker_at_the_arp_address(tmp_path: Path) -> None:
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    other = _speaker_zone("RINCON_BBBBBBBBBBBB01400", "192.168.1.10", "Kitchen")
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.10"),
        patch("app.sonos_device_manager.SoCo", return_value=other),
    ):
        assert await mgr.recover_unresponsive() == ()
    assert mgr.players[0].unresponsive is True
    assert mgr.players[0].rincon_uid == _LIVING_ROOM_UID


@pytest.mark.asyncio
async def test_recover_unresponsive_sweep_finds_a_speaker_arp_does_not_know(tmp_path: Path) -> None:
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    moved = _speaker_zone(_LIVING_ROOM_UID, "192.168.1.30")
    silent = MagicMock(ip_address="192.168.1.10")
    silent.get_speaker_info.side_effect = TimeoutError("no answer")
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.sonos_device_manager.SoCo", return_value=silent),
        patch("app.sonos_device_manager.soco_discover", return_value={moved}),
    ):
        assert await mgr.recover_unresponsive() == ()
        assert await mgr.recover_unresponsive(sweep=True) == (_LIVING_ROOM_MAC,)
    assert mgr.players[0].host == "192.168.1.30"


@pytest.mark.asyncio
async def test_forget_device_removes_the_speaker_from_the_roster_and_the_cache(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr, db = await _manager_with_a_missing_speaker(tmp_path, store)
    store.note_disappeared(device_id=_LIVING_ROOM_MAC, family_id="sonos", host="192.168.1.10", label="Living Room")
    assert await mgr.forget_device("AA:AA:AA:AA:AA:AA") is True
    assert mgr.players == ()
    assert device_discovery_store.load_sonos_zones(db) == []
    assert store.notices() == ()
    assert await mgr.forget_device(_LIVING_ROOM_MAC) is False


@pytest.mark.asyncio
async def test_forget_device_keeps_the_other_speakers(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, presence_store=DevicePresenceStore())
    a = _speaker_zone(_LIVING_ROOM_UID, "192.168.1.10", "Living Room")
    b = _speaker_zone("RINCON_BBBBBBBBBBBB01400", "192.168.1.11", "Kitchen")
    with patch("app.sonos_device_manager.soco_discover", return_value={a, b}):
        await mgr.fetch()
    assert await mgr.forget_device("bb:bb:bb:bb:bb:bb") is True
    assert [p.mac_address for p in mgr.players] == [_LIVING_ROOM_MAC]
    assert [row[0] for row in device_discovery_store.load_sonos_zones(db)] == [_LIVING_ROOM_UID]


@pytest.mark.asyncio
async def test_recover_unresponsive_does_not_trust_a_stale_cached_uid_at_a_reused_address(tmp_path: Path) -> None:
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    # SoCo keeps one instance per address, so ``uid`` can still hold the old speaker's value
    # while the live device description already belongs to the speaker that took the address.
    reused = _speaker_zone("RINCON_BBBBBBBBBBBB01400", "192.168.1.10", "Kitchen")
    reused.uid = _LIVING_ROOM_UID
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.10"),
        patch("app.sonos_device_manager.SoCo", return_value=reused),
    ):
        assert await mgr.recover_unresponsive() == ()
    assert mgr.players[0].unresponsive is True


@pytest.mark.asyncio
async def test_recover_unresponsive_sweep_does_not_trust_a_stale_cached_uid(tmp_path: Path) -> None:
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    reused = _speaker_zone("RINCON_BBBBBBBBBBBB01400", "192.168.1.10", "Kitchen")
    reused.uid = _LIVING_ROOM_UID
    silent = MagicMock(ip_address="192.168.1.10")
    silent.get_speaker_info.side_effect = TimeoutError("no answer")
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value=None),
        patch("app.sonos_device_manager.SoCo", return_value=silent),
        patch("app.sonos_device_manager.soco_discover", return_value={reused}),
    ):
        assert await mgr.recover_unresponsive(sweep=True) == ()
    assert mgr.players[0].unresponsive is True
    assert mgr.players[0].rincon_uid == _LIVING_ROOM_UID


@pytest.mark.asyncio
async def test_forget_device_failing_to_persist_leaves_the_roster_and_the_notice_untouched(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, store)
    store.note_disappeared(device_id=_LIVING_ROOM_MAC, family_id="sonos", host="192.168.1.10", label="Living Room")
    with (
        patch("app.sonos_device_manager.device_discovery_store.save_sonos_zones", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await mgr.forget_device(_LIVING_ROOM_MAC)
    assert [p.mac_address for p in mgr.players] == [_LIVING_ROOM_MAC]
    assert len(store.notices()) == 1


@pytest.mark.asyncio
async def test_recover_unresponsive_failing_to_persist_keeps_the_stub(tmp_path: Path) -> None:
    mgr, _db = await _manager_with_a_missing_speaker(tmp_path, DevicePresenceStore())
    with (
        patch("app.sonos_device_manager.lookup_ip_via_arp_for_mac", return_value="192.168.1.20"),
        patch("app.sonos_device_manager.SoCo", return_value=_speaker_zone(_LIVING_ROOM_UID, "192.168.1.20")),
        patch("app.sonos_device_manager.device_discovery_store.save_sonos_zones", side_effect=OSError("disk full")),
        pytest.raises(OSError),
    ):
        await mgr.recover_unresponsive()
    assert mgr.players[0].unresponsive is True
    assert mgr.players[0].host == "192.168.1.10"


def test_persist_cache_falls_back_to_the_known_label_when_the_live_name_read_fails(tmp_path: Path) -> None:
    db = tmp_path / "sonos.sqlite"
    mgr = SonosDeviceManager(discovery_timeout=0.1, discovery_cache_path=db, presence_store=DevicePresenceStore())
    zone = _speaker_zone(_LIVING_ROOM_UID, "192.168.1.10")
    type(zone).player_name = property(lambda _self: (_ for _ in ()).throw(TimeoutError("no answer")))
    speaker = SonosSpeakerDevice(
        _LIVING_ROOM_MAC, zone, display_name="Living Room", mac_address=_LIVING_ROOM_MAC, rincon_uid=_LIVING_ROOM_UID
    )
    mgr._persist_cache([speaker])
    assert device_discovery_store.load_sonos_zones(db) == [
        (_LIVING_ROOM_UID, "192.168.1.10", "Living Room", _LIVING_ROOM_MAC)
    ]
