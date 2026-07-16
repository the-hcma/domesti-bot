"""Tests for :mod:`androidtv_device_manager` (Google Cast / PyChromecast, no hardware)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch
from uuid import UUID

import pytest

from app import device_discovery_store
from app.androidtv_device_manager import (
    AndroidTvDeviceManager,
    AndroidTvSwitchDevice,
    _discover_cast_infos_sync,
    _host_hint_from_spec,
    _merge_androidtv_host_specs,
    discover_cast_adb_specs_via_zeroconf,
)


def test_discover_cast_infos_sync_does_not_emit_pychromecast_deprecation_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Regression: ``discover_chromecasts`` logged a noisy INFO every cold
    start ("discover_chromecasts is deprecated and will be removed in June
    2024 …"). We now call :class:`CastBrowser` directly, so that line must
    not appear in the log when ``_discover_cast_infos_sync`` runs.
    """

    fake_browser = MagicMock()
    fake_browser.devices = {}
    fake_browser.count = 0
    fake_browser.start_discovery = MagicMock()
    fake_browser.stop_discovery = MagicMock()

    with (
        patch(
            "app.androidtv_device_manager.zeroconf.Zeroconf",
            return_value=MagicMock(),
        ),
        patch(
            "app.androidtv_device_manager.CastBrowser",
            return_value=fake_browser,
        ),
        caplog.at_level(logging.INFO),
    ):
        infos, browser = _discover_cast_infos_sync(timeout=0.05, known_hosts=None)

    assert infos == []
    assert browser is fake_browser
    fake_browser.start_discovery.assert_called_once()
    deprecation_msgs = [r.getMessage() for r in caplog.records if "deprecated" in r.getMessage().lower()]
    assert deprecation_msgs == [], deprecation_msgs


def test_discover_cast_infos_sync_short_circuits_when_all_known_hosts_resolve() -> None:
    """When ``known_hosts`` is supplied, the wait must end as soon as that many
    Cast records have been seen (rather than always burning the full timeout).
    """

    fake_browser = MagicMock()
    fake_browser.devices = {UUID(int=1): MagicMock(), UUID(int=2): MagicMock()}
    fake_browser.count = 0

    def _arm(listener: object, _zc: object, _kh: object) -> MagicMock:
        # Simulate two add-callback fires inside start_discovery so the
        # threading.Event flips to "done" before the timeout elapses.
        def _start() -> None:
            for _ in range(2):
                fake_browser.count += 1
                listener.add_cast(UUID(int=fake_browser.count), "service")  # type: ignore[attr-defined]

        fake_browser.start_discovery.side_effect = _start
        return fake_browser

    with (
        patch(
            "app.androidtv_device_manager.zeroconf.Zeroconf",
            return_value=MagicMock(),
        ),
        patch(
            "app.androidtv_device_manager.CastBrowser",
            side_effect=_arm,
        ),
    ):
        # Generous timeout — if early-termination works the call returns
        # in milliseconds, not 5 s.
        import time as _t

        started = _t.perf_counter()
        infos, _browser = _discover_cast_infos_sync(timeout=5.0, known_hosts=["10.0.0.1", "10.0.0.2"])
        elapsed = _t.perf_counter() - started

    assert len(infos) == 2
    assert elapsed < 1.0, f"early-termination broken: waited {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_discover_cast_returns_rows_and_labels() -> None:
    uid = UUID("12345678-1234-5678-1234-567812345678")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "Kitchen TV"
    info.host = "10.0.0.9"
    info.port = 8009
    browser = MagicMock()

    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info], browser),
    ):
        uuids, labels, rows = await discover_cast_adb_specs_via_zeroconf(timeout=1.0)
    assert uuids == [str(uid)]
    assert labels[str(uid)] == "Kitchen TV"
    assert rows == [("10.0.0.9", 8009, "Kitchen TV")]


@pytest.mark.asyncio
async def test_fetch_connects_discovered_casts() -> None:
    uid = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "Office"
    info.host = "192.168.1.20"
    info.port = 8009

    cast = MagicMock()
    cast.socket_client.host = "192.168.1.20"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False

    browser = MagicMock()
    browser.zc = MagicMock()

    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info], browser),
    ):
        with patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ):
            mgr = AndroidTvDeviceManager([], zeroconf_discovery=True)
            await mgr.fetch()

    devices = list(mgr.switches)
    assert len(devices) == 1
    assert devices[0].identifier == str(uid)
    assert devices[0].preferred_label == "Office"


@pytest.mark.asyncio
async def test_fetch_skips_mdns_when_sqlite_cache_fully_named(tmp_path) -> None:
    db = tmp_path / "disc.sqlite"
    device_discovery_store.save_androidtv_hosts(db, [("10.0.0.77", 8009, "Master Cached")])

    uid = UUID("11111111-2222-3333-4444-555555555555")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "Master Cached"
    info.host = "10.0.0.77"
    info.port = 8009

    cast = MagicMock()
    cast.socket_client.host = "10.0.0.77"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False
    browser = MagicMock()
    browser.zc = MagicMock()

    mgr = AndroidTvDeviceManager(
        [],
        discovery_store_path=db,
        zeroconf_discovery=True,
    )
    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info], browser),
    ) as zc_fn:
        with patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ):
            await mgr.fetch()
    zc_fn.assert_called_once()
    call_kw = zc_fn.call_args.kwargs
    assert call_kw["known_hosts"] == ["10.0.0.77"]


@pytest.mark.asyncio
async def test_rediscover_invokes_full_browse_with_cache(tmp_path) -> None:
    db = tmp_path / "disc.sqlite"
    device_discovery_store.save_androidtv_hosts(db, [("10.0.0.77", 8009, "TV")])

    uid = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "TV"
    info.host = "10.0.0.77"
    info.port = 8009
    cast = MagicMock()
    cast.socket_client.host = "10.0.0.77"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False
    browser = MagicMock()
    browser.zc = MagicMock()

    mgr = AndroidTvDeviceManager(
        [],
        discovery_store_path=db,
        zeroconf_discovery=True,
    )
    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info], browser),
    ) as zc_fn:
        with patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ):
            await mgr.fetch()
            zc_fn.reset_mock()
            await mgr.rediscover()
    assert zc_fn.call_count >= 1
    full_calls = [c for c in zc_fn.call_args_list if c.kwargs.get("known_hosts") is None]
    assert full_calls


@pytest.mark.asyncio
async def test_fetch_runs_mdns_when_cache_missing_friendly_names(tmp_path) -> None:
    db = tmp_path / "disc.sqlite"
    device_discovery_store.save_androidtv_hosts(db, [("10.0.0.5", 8009)])

    uid = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "X"
    info.host = "10.0.0.5"
    info.port = 8009
    cast = MagicMock()
    cast.socket_client.host = "10.0.0.5"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False
    browser = MagicMock()
    browser.zc = MagicMock()

    mgr = AndroidTvDeviceManager(
        [],
        discovery_store_path=db,
        zeroconf_discovery=True,
    )
    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info], browser),
    ) as zc_fn:
        with patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ):
            await mgr.fetch()
    open_calls = [c for c in zc_fn.call_args_list if c.kwargs.get("known_hosts") is None]
    assert open_calls


@pytest.mark.asyncio
async def test_fetch_uses_no_mdns_fast_path_when_cache_has_uuids(tmp_path) -> None:
    """Every cached row carrying a UUID must skip mDNS entirely and use
    :func:`pychromecast.get_chromecast_from_host` for a parallel connect."""

    db = tmp_path / "atv.sqlite"
    uid_a = "11111111-2222-3333-4444-555555555555"
    uid_b = "66666666-7777-8888-9999-aaaaaaaaaaaa"
    device_discovery_store.save_androidtv_hosts(
        db,
        [
            ("10.0.0.10", 8009, "Living Room", uid_a, "Chromecast"),
            ("10.0.0.11", 8009, "Kitchen", uid_b, "Nest Audio"),
        ],
    )

    casts: dict[str, MagicMock] = {}

    def fake_from_host(host_tuple: tuple, **_kwargs: object) -> MagicMock:
        host, port, host_uuid, model, friendly = host_tuple
        cc = MagicMock()
        cc.socket_client.host = host
        cc.socket_client.port = port
        cc.media_controller.status.player_is_playing = False
        casts[str(host_uuid)] = cc
        return cc

    mgr = AndroidTvDeviceManager([], discovery_store_path=db, zeroconf_discovery=True)
    with (
        patch("app.androidtv_device_manager._discover_cast_infos_sync") as zc_fn,
        patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_host",
            side_effect=fake_from_host,
        ) as host_fn,
    ):
        await mgr.fetch()

    zc_fn.assert_not_called()  # the smoking-gun assertion: zero mDNS traffic.
    assert host_fn.call_count == 2
    ids = sorted(d.identifier for d in mgr.switches)
    assert ids == sorted([uid_a, uid_b])
    assert mgr.last_discovery_source == "cache"


@pytest.mark.asyncio
async def test_fetch_fast_path_drops_unreachable_cached_device(tmp_path) -> None:
    """A dead host in the cache must be skipped, **not** trigger a mDNS fallback."""

    db = tmp_path / "atv.sqlite"
    uid_live = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    uid_dead = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    device_discovery_store.save_androidtv_hosts(
        db,
        [
            ("10.0.0.10", 8009, "Live", uid_live, None),
            ("10.0.0.99", 8009, "Dead", uid_dead, None),
        ],
    )

    def fake_from_host(host_tuple: tuple, **_kwargs: object) -> MagicMock:
        host, port, _u, _m, _fn = host_tuple
        if host == "10.0.0.99":
            raise OSError("offline")
        cc = MagicMock()
        cc.socket_client.host = host
        cc.socket_client.port = port
        cc.media_controller.status.player_is_playing = False
        return cc

    mgr = AndroidTvDeviceManager([], discovery_store_path=db, zeroconf_discovery=True)
    with (
        patch("app.androidtv_device_manager._discover_cast_infos_sync") as zc_fn,
        patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_host",
            side_effect=fake_from_host,
        ),
    ):
        await mgr.fetch()

    zc_fn.assert_not_called()
    # Only the live device survives; the dead one is silently dropped.
    assert [d.identifier for d in mgr.switches] == [uid_live]
    assert mgr.last_discovery_source == "cache"


@pytest.mark.asyncio
async def test_fetch_falls_back_to_mdns_when_any_cache_row_missing_uuid(tmp_path) -> None:
    """A single legacy row (uuid IS NULL) must invalidate the fast path."""

    db = tmp_path / "atv.sqlite"
    uid_full = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    device_discovery_store.save_androidtv_hosts(
        db,
        [
            ("10.0.0.10", 8009, "Has UUID", uid_full, "Chromecast"),
            ("10.0.0.20", 8009, "Pre-migration"),  # 3-tuple → uuid IS NULL
        ],
    )

    info = MagicMock()
    info.uuid = UUID(uid_full)
    info.friendly_name = "Has UUID"
    info.host = "10.0.0.10"
    info.port = 8009
    info.model_name = "Chromecast"
    cast = MagicMock()
    cast.socket_client.host = "10.0.0.10"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False
    browser = MagicMock()
    browser.zc = MagicMock()

    mgr = AndroidTvDeviceManager([], discovery_store_path=db, zeroconf_discovery=True)
    with (
        patch(
            "app.androidtv_device_manager._discover_cast_infos_sync",
            return_value=([info], browser),
        ) as zc_fn,
        patch("app.androidtv_device_manager.pychromecast.get_chromecast_from_host") as host_fn,
        patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ),
    ):
        await mgr.fetch()

    zc_fn.assert_called_once()  # mDNS ran because one cache row lacked a UUID.
    host_fn.assert_not_called()
    assert mgr.last_discovery_source == "discovery"


@pytest.mark.asyncio
async def test_fetch_mdns_path_persists_uuids_for_next_fast_path(tmp_path) -> None:
    """After a fresh mDNS sweep the cache must contain UUID + model_name so the
    *next* startup hits the no-mDNS fast path."""

    db = tmp_path / "atv.sqlite"
    uid = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    info = MagicMock()
    info.uuid = uid
    info.friendly_name = "Den"
    info.host = "10.0.0.50"
    info.port = 8009
    info.model_name = "Nest Audio"
    cast = MagicMock()
    cast.socket_client.host = "10.0.0.50"
    cast.socket_client.port = 8009
    cast.media_controller.status.player_is_playing = False
    browser = MagicMock()
    browser.zc = MagicMock()

    mgr = AndroidTvDeviceManager([], discovery_store_path=db, zeroconf_discovery=True)
    with (
        patch(
            "app.androidtv_device_manager._discover_cast_infos_sync",
            return_value=([info], browser),
        ),
        patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            return_value=cast,
        ),
    ):
        await mgr.fetch()

    assert mgr.last_discovery_source == "discovery"
    rows = device_discovery_store.load_androidtv_known_devices(db)
    assert rows == [("10.0.0.50", 8009, "Den", str(uid), "Nest Audio")]


@pytest.mark.asyncio
async def test_fetch_connect_skips_failed_hosts() -> None:
    uid_ok = UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
    info_ok = MagicMock()
    info_ok.uuid = uid_ok
    info_ok.friendly_name = "Good"
    info_ok.host = "good.host"
    info_ok.port = 8009

    uid_bad = UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
    info_bad = MagicMock()
    info_bad.uuid = uid_bad
    info_bad.friendly_name = "Bad"
    info_bad.host = "bad.host"
    info_bad.port = 8009

    cast_ok = MagicMock()
    cast_ok.socket_client.host = "good.host"
    cast_ok.socket_client.port = 8009
    cast_ok.media_controller.status.player_is_playing = True
    browser = MagicMock()
    browser.zc = MagicMock()

    def fake_get(info: MagicMock, _zc: object, **kwargs: object) -> MagicMock:
        if info.host == "bad.host":
            raise OSError("nope")
        return cast_ok

    mgr = AndroidTvDeviceManager(["bad.host", "good.host"])
    with patch(
        "app.androidtv_device_manager._discover_cast_infos_sync",
        return_value=([info_bad, info_ok], browser),
    ):
        with patch(
            "app.androidtv_device_manager.pychromecast.get_chromecast_from_cast_info",
            side_effect=fake_get,
        ):
            await mgr.fetch()

    devices = list(mgr.switches)
    assert len(devices) == 1
    assert devices[0].identifier == str(uid_ok)


def test_zeroconf_discovery_wanted_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANDROIDTV_ZEROCONF", raising=False)
    assert AndroidTvDeviceManager.zeroconf_discovery_wanted() is True


@pytest.mark.parametrize("raw", ("0", "false", "no", "off", "OFF"))
def test_zeroconf_discovery_wanted_env_off(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("ANDROIDTV_ZEROCONF", raw)
    assert AndroidTvDeviceManager.zeroconf_discovery_wanted() is False


def test_zeroconf_discovery_wanted_cli_opt_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANDROIDTV_ZEROCONF", raising=False)
    assert AndroidTvDeviceManager.zeroconf_discovery_wanted(cli_opt_out=True) is False


def test_merge_androidtv_host_specs_dedupes_and_strips(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANDROIDTV_HOSTS", "10.0.0.1,10.0.0.1:5555, 10.0.0.1 ")
    got = _merge_androidtv_host_specs(["10.0.0.2", "10.0.0.2"])
    assert got == ["10.0.0.2", "10.0.0.1", "10.0.0.1:5555"]


def test_host_hint_from_spec_ipv4_with_port() -> None:
    assert _host_hint_from_spec("192.168.1.10:5555") == "192.168.1.10"


def test_host_hint_from_spec_ipv6_bracketed() -> None:
    assert _host_hint_from_spec("[2001:db8::1]:8009") == "2001:db8::1"


@pytest.mark.asyncio
async def test_switch_refresh_reads_player_is_playing() -> None:
    cast = MagicMock()
    cast.media_controller.status.player_is_playing = True
    dev = AndroidTvSwitchDevice("uuid-here", cast, display_name="TV")
    await dev.refresh_power_state()
    assert dev.is_on is True


@pytest.mark.asyncio
async def test_turn_off_calls_stop_when_session_active() -> None:
    cast = MagicMock()
    cast.media_controller.status.media_session_id = 7
    cast.media_controller.status.player_is_playing = False
    dev = AndroidTvSwitchDevice("u1", cast)
    await dev.turn_off()
    cast.media_controller.stop.assert_called_once()
    cast.quit_app.assert_called_once()
    cast.disconnect.assert_not_called()


@pytest.mark.asyncio
async def test_turn_off_disconnects_when_host_tuple_then_turn_on_reconnects() -> None:
    uid = UUID("12345678-1234-5678-1234-567812345678")
    cast1 = MagicMock()
    cast1.media_controller.status.media_session_id = None
    cast1.media_controller.status.player_is_playing = False
    cast2 = MagicMock()
    st = MagicMock()
    st.media_session_id = 1
    st.player_is_paused = True
    st.player_is_playing = False
    cast2.media_controller.status = st
    dev = AndroidTvSwitchDevice(
        str(uid),
        cast1,
        connect_timeout=1.0,
        display_name="TV",
        host_connect_tuple=("10.0.0.1", 8009, uid, None, "TV"),
    )
    await dev.turn_off()
    cast1.media_controller.stop.assert_not_called()
    cast1.quit_app.assert_called_once()
    cast1.disconnect.assert_called_once()

    with patch(
        "app.androidtv_device_manager.pychromecast.get_chromecast_from_host",
        return_value=cast2,
    ) as gch:
        await dev.turn_on()
    gch.assert_called_once()
    assert dev._cast is cast2
    cast2.media_controller.play.assert_called_once()


@pytest.mark.asyncio
async def test_turn_on_play_when_paused() -> None:
    cast = MagicMock()
    st = MagicMock()
    st.media_session_id = 1
    st.player_is_paused = True
    st.player_is_playing = False
    cast.media_controller.status = st
    dev = AndroidTvSwitchDevice("u2", cast)
    await dev.turn_on()
    cast.media_controller.play.assert_called_once()
