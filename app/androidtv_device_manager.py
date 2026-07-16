"""Google Cast targets (Chromecast, Google TV, Nest Audio, …) via **PyChromecast**.

There is **no ADB**. ``is-on`` / cached power treat **active media playback** (playing or
buffering) as *on* — idle or paused reads as *off* for this proxy. ``turn-off`` sends **STOP**
when a media session exists, then **quit_app** to tear down the receiver, then
**disconnect** so the Chromecast fully idles (mirrors common Cast client shutdown). A cached
host tuple reconnects lazily on the next ``turn-on`` / ``is-on`` / status refresh. Mocks and
callers without endpoint metadata skip **disconnect** (no way to reconnect).
``turn-on`` resumes with **PLAY** only when the session is **paused**; otherwise it is a
no-op that refreshes status (there is no generic Cast “power on”).

**Discovery:** mDNS ``_googlecast._tcp`` through PyChromecast. Optional ``ANDROIDTV_HOSTS`` /
``--androidtv-host`` supply **known host** hints (IP or hostname; port ignored for hints) to
speed discovery. SQLite caches the last-seen **(host, port, friendly_name)** row per device;
device identifiers in the REPL are **Cast UUID** strings.

``--no-androidtv-zeroconf`` / ``ANDROIDTV_ZEROCONF=0`` limits discovery to explicit and cached
hosts only (no open-ended LAN browse).

``refresh-discovery`` / :meth:`~AndroidTvDeviceManager.rediscover` always runs a full browse
when zeroconf is enabled.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import pychromecast
import zeroconf
from pychromecast import Chromecast
from pychromecast.discovery import CastBrowser, SimpleCastListener
from pychromecast.models import CastInfo

from app import device_discovery_store
from app.device_manager import AlreadyInitializedError, NotInitializedError, SwitchDeviceManager
from app.rule_engine import SwitchDevice

_LOGGER = logging.getLogger(__name__)

# TODO(google-cast-on-off): The Cast control path (``turn_on`` /
# ``turn_off``) currently turns devices on but never reliably turns
# them off again — observed end-to-end against the household devices.
# Until that is investigated, ``bootstrap_device_managers`` skips
# AndroidTV bring-up entirely so a Cast tile never appears in the
# UI and the REPL ``androidtv`` family is empty. The manager class,
# discovery helpers, and tests remain in place so the investigation
# can flip this flag back to ``False`` without recreating scaffolding.
# When the on/off path is verified, remove this constant *and* the
# short-circuit in ``app.domesti_bot_cli.bootstrap_device_managers``.
ANDROIDTV_TEMPORARILY_DISABLED = True
ANDROIDTV_TEMPORARILY_DISABLED_REASON = (
    "temporarily disabled — TODO(google-cast-on-off): Cast turn_off path is unreliable; investigate before re-enabling"
)

_DEFAULT_CAST_PORT = 8009
_STATUS_POLL_SLEEP_S = 0.35
# Per-device timeout for the no-mDNS cache-fast path. Healthy Chromecasts
# answer in under a second; this caps the cost of an offline device on a warm
# start so a dead row adds ~2 s instead of the full ``connection_timeout`` (20 s
# default, used by the mDNS-driven fresh-discovery path).
_CACHE_FAST_CONNECT_TIMEOUT_S = 2.0


def _dedupe_strs(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in items:
        s = raw.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def _host_hint_from_spec(spec: str) -> str:
    """Strip optional trailing ``:port`` for Cast ``known_hosts`` hints."""

    s = spec.strip()
    if not s:
        return ""
    if s.startswith("["):
        closing = s.find("]")
        if closing < 1:
            return s
        inner = s[1:closing]
        tail = s[closing + 1 :].strip()
        if tail.startswith(":"):
            return inner
        return inner
    if ":" in s:
        host_part, _, port_part = s.rpartition(":")
        if port_part.isdigit():
            return host_part.strip()
    return s


def _discover_cast_infos_sync(
    *,
    timeout: float,
    known_hosts: list[str] | None,
) -> tuple[list[CastInfo], CastBrowser]:
    """Run blocking Cast mDNS discovery; caller MUST call ``browser.stop_discovery()``.

    Inlines the deprecated :func:`pychromecast.discovery.discover_chromecasts`
    wrapper (which logged a noisy deprecation INFO on every cold start). Uses
    :class:`CastBrowser` directly per the pychromecast 14.x guidance:
    construct a ``CastBrowser(SimpleCastListener(...), zeroconf_instance,
    known_hosts)``, call ``start_discovery()``, wait ``timeout`` seconds, and
    return ``browser.devices.values()``. When ``known_hosts`` is provided we
    short-circuit as soon as that many Cast records have been observed so the
    cache-warm-but-uuid-less recovery path doesn't pay a full 12 s sleep when
    every hint resolves immediately.
    """

    kh = known_hosts if known_hosts else None
    expected = len(kh) if kh else None
    done = threading.Event()

    def _on_add(_uuid: object, _service: str) -> None:
        if expected is not None and browser.count >= expected:
            done.set()

    zconf = zeroconf.Zeroconf()
    browser = CastBrowser(SimpleCastListener(_on_add), zconf, kh)
    browser.start_discovery()
    done.wait(max(0.5, float(timeout)))
    return (list(browser.devices.values()), browser)


def _merge_androidtv_host_specs(cli_hosts: list[str]) -> list[str]:
    """CLI/env merge for host hints: ``ANDROIDTV_HOSTS`` comma list plus CLI repeats."""

    merged = list(cli_hosts)
    env = (os.environ.get("ANDROIDTV_HOSTS") or "").strip()
    if env:
        merged.extend(part.strip() for part in env.split(",") if part.strip())
    return _dedupe_strs(merged)


async def discover_cast_adb_specs_via_zeroconf(
    *,
    timeout: float = 12.0,
    adb_port: int = _DEFAULT_CAST_PORT,
    known_hosts: list[str] | None = None,
) -> tuple[list[str], dict[str, str], list[tuple[str, int, str | None]]]:
    """Discover Cast devices.

    Returns ``(uuid_strings, uuid -> friendly label, sqlite_rows)`` where each SQLite row is
    ``(cast_host, cast_port, friendly_name)`` for :func:`device_discovery_store.save_androidtv_hosts`.

    The ``adb_port`` parameter is **ignored** (kept for call-site compatibility with the old
    ADB stack).
    """

    _ = adb_port

    def sync() -> tuple[list[str], dict[str, str], list[tuple[str, int, str | None]], Any]:
        infos, browser = _discover_cast_infos_sync(timeout=timeout, known_hosts=known_hosts)
        try:
            uuids: list[str] = []
            labels: dict[str, str] = {}
            rows: list[tuple[str, int, str | None]] = []
            for info in infos:
                uid = str(info.uuid)
                uuids.append(uid)
                fn = (info.friendly_name or "").strip()
                if fn:
                    labels[uid] = fn
                rows.append(
                    (
                        str(info.host).strip(),
                        int(info.port) if info.port else _DEFAULT_CAST_PORT,
                        fn or None,
                    )
                )
            uuids.sort()
            rows.sort(key=lambda t: (t[0], t[1]))
            return (uuids, labels, rows, browser)
        except BaseException:
            with contextlib.suppress(Exception):
                browser.stop_discovery()
            raise

    uuids, labels, rows, browser = await asyncio.to_thread(sync)
    with contextlib.suppress(Exception):
        browser.stop_discovery()
    return (uuids, labels, rows)


class AndroidTvSwitchDevice(SwitchDevice):
    """Cast-backed switch: *on* means media **playing**; *off* stops playback."""

    __slots__ = ("_cast", "_connect_timeout", "_host_connect_tuple")

    def __init__(
        self,
        identifier: str,
        cast: Chromecast,
        *,
        connect_timeout: float = 20.0,
        display_name: str | None = None,
        host_connect_tuple: tuple[str, int, uuid.UUID, str | None, str | None] | None = None,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._cast = cast
        self._connect_timeout = float(connect_timeout)
        self._host_connect_tuple = host_connect_tuple

    def _ensure_cast_connected(self) -> None:
        """Recreate the PyChromecast socket after :meth:`turn_off` disconnected."""

        if self._cast is not None:
            return
        tup = self._host_connect_tuple
        if tup is None:
            raise RuntimeError("Expected host_connect_tuple for reconnect after disconnect, got None")
        cc = pychromecast.get_chromecast_from_host(
            tup,
            tries=1,
            timeout=self._connect_timeout,
        )
        cc.wait(timeout=self._connect_timeout)
        self._cast = cc

    async def refresh_power_state(self) -> None:
        def sync() -> None:
            self._ensure_cast_connected()
            try:
                self._cast.media_controller.update_status()
                time.sleep(_STATUS_POLL_SLEEP_S)
            except Exception:
                pass
            st = self._cast.media_controller.status
            self.set_power(bool(st.player_is_playing))

        await asyncio.to_thread(sync)

    async def turn_off(self) -> None:
        def sync() -> None:
            self._ensure_cast_connected()
            try:
                self._cast.media_controller.update_status()
                time.sleep(_STATUS_POLL_SLEEP_S)
            except Exception:
                pass
            try:
                if self._cast.media_controller.status.media_session_id is not None:
                    self._cast.media_controller.stop(timeout=12.0)
            except Exception as ex:
                _LOGGER.debug("Cast STOP failed (%s); continuing to quit_app", ex)
            with contextlib.suppress(Exception):
                self._cast.quit_app(timeout=12.0)
            if self._host_connect_tuple is not None:
                with contextlib.suppress(Exception):
                    self._cast.disconnect(timeout=3.0)
                self._cast = None
                self.set_power(False)
                return
            try:
                self._cast.media_controller.update_status()
                time.sleep(_STATUS_POLL_SLEEP_S)
            except Exception:
                pass
            st = self._cast.media_controller.status
            self.set_power(bool(st.player_is_playing))

        await asyncio.to_thread(sync)

    async def turn_on(self) -> None:
        def sync() -> None:
            self._ensure_cast_connected()
            try:
                self._cast.media_controller.update_status()
                time.sleep(_STATUS_POLL_SLEEP_S)
            except Exception:
                pass
            st = self._cast.media_controller.status
            if st.player_is_paused and st.media_session_id is not None:
                with contextlib.suppress(Exception):
                    self._cast.media_controller.play(timeout=12.0)
                time.sleep(_STATUS_POLL_SLEEP_S)
            st2 = self._cast.media_controller.status
            self.set_power(bool(st2.player_is_playing))

        await asyncio.to_thread(sync)


class AndroidTvDeviceManager(SwitchDeviceManager[AndroidTvSwitchDevice]):
    """PyChromecast discovery + SQLite cache of Cast endpoints (host/port/friendly name)."""

    def __init__(
        self,
        explicit_host_specs: list[str],
        *,
        connection_timeout: float = 20.0,
        discovery_store_path: Path | str | None = None,
        zeroconf_discovery: bool = True,
        zeroconf_timeout: float = 12.0,
    ) -> None:
        self._alias_to_device: dict[str, AndroidTvSwitchDevice] | None = None
        self._connection_timeout = float(connection_timeout)
        self._discovery_store_path = Path(discovery_store_path).expanduser().resolve() if discovery_store_path else None
        self._explicit_specs = _dedupe_strs(list(explicit_host_specs))
        self._zeroconf_discovery = bool(zeroconf_discovery)
        self._zeroconf_timeout = float(zeroconf_timeout)
        # Set by :meth:`fetch` to ``"cache"`` (targeted mDNS probe of known
        # hosts only — no LAN-wide sweep) or ``"discovery"`` (full
        # zeroconf browse). ``None`` before the first ``fetch``.
        self._last_discovery_source: str | None = None

    def __str__(self) -> str:
        if self._alias_to_device is None:
            return "AndroidTvDeviceManager(not initialized)"
        uniq = list({id(d): d for d in self._alias_to_device.values()}.values())
        uniq.sort(key=lambda d: d.preferred_label.lower())
        lines = ["AndroidTvDeviceManager (Google Cast):"]
        for dev in uniq:
            lines.append(f"  {dev.preferred_label!r} ({dev.identifier}) — proxy power {dev.power_state}")
        return "\n".join(lines)

    def _device_for(self, identifier: str) -> AndroidTvSwitchDevice:
        if self._alias_to_device is None:
            raise NotInitializedError
        d = self._alias_to_device.get(identifier)
        if d is None:
            raise ValueError(f"Unknown Cast device: {identifier!r}")
        return d

    def _discovery_browse_known_hosts(
        self,
        *,
        full_zeroconf: bool,
    ) -> tuple[str, list[str]]:
        """``(\"full\", [])`` for LAN-wide browse, or ``(\"hints_only\", hosts)`` for targeted."""

        cached_rows = self._load_cached_androidtv_rows()
        hints = self._known_hosts_for_fetch(cached_rows)
        if not self._zeroconf_discovery:
            return ("hints_only", hints)
        if full_zeroconf:
            return ("full", [])
        if cached_rows and all((fn or "").strip() for _, _, fn in cached_rows) and hints:
            return ("hints_only", hints)
        return ("full", [])

    def _expand_lookup(self, devices: list[AndroidTvSwitchDevice]) -> dict[str, AndroidTvSwitchDevice]:
        alias_map: dict[str, AndroidTvSwitchDevice] = {}
        for dev in devices:
            alias_map[dev.identifier] = dev
            label = dev.preferred_label
            if label != dev.identifier:
                alias_map[label] = dev
        return alias_map

    def _finalize_devices(self, uniq: list[AndroidTvSwitchDevice]) -> None:
        if self._discovery_store_path is not None:
            for backend, key, disp in device_discovery_store.load_display_names(self._discovery_store_path):
                if backend != "androidtv":
                    continue
                for dev in uniq:
                    if dev.identifier == key:
                        dev.set_display_name(disp)
                        break
        self._alias_to_device = self._expand_lookup(uniq)

    def _known_hosts_for_fetch(
        self,
        cached_rows: list[tuple[str, int, str | None]],
    ) -> list[str]:
        hints: list[str] = []
        for spec in self._explicit_specs:
            h = _host_hint_from_spec(spec)
            if h:
                hints.append(h)
        for host, _port, _fn in cached_rows:
            hs = str(host).strip()
            if hs:
                hints.append(hs)
        return _dedupe_strs(hints)

    def _load_cached_androidtv_rows(self) -> list[tuple[str, int, str | None]]:
        if self._discovery_store_path is None:
            return []
        return device_discovery_store.load_androidtv_endpoint_rows(self._discovery_store_path)

    async def disconnect(self) -> None:
        if self._alias_to_device is None:
            return
        cached = self._alias_to_device
        self._alias_to_device = None
        seen: set[int] = set()
        for dev in cached.values():
            cast = dev._cast
            if cast is None:
                continue
            cid = id(cast)
            if cid in seen:
                continue
            seen.add(cid)

            def _dc(c: Chromecast = cast) -> None:
                with contextlib.suppress(Exception):
                    c.disconnect(timeout=3.0)

            await asyncio.to_thread(_dc)

    async def discover_hosts(self) -> list[str]:
        """Multicast-discover Cast devices; return sorted **UUID** strings."""

        mode, hints = self._discovery_browse_known_hosts(full_zeroconf=True)
        if mode == "hints_only" and not hints:
            return []

        def run(kh: list[str] | None) -> list[str]:
            infos, browser = _discover_cast_infos_sync(
                timeout=self._zeroconf_timeout,
                known_hosts=kh,
            )
            try:
                return sorted(str(i.uuid) for i in infos)
            finally:
                with contextlib.suppress(Exception):
                    browser.stop_discovery()

        kh: list[str] | None = None if mode == "full" else hints
        return await asyncio.to_thread(run, kh)

    async def _connect_devices_from_cache(
        self,
        known: list[tuple[str, int, str | None, str | None, str | None]],
    ) -> list[AndroidTvSwitchDevice]:
        """No-mDNS connect path: build a ``Chromecast`` per cached row using
        ``pychromecast.get_chromecast_from_host`` (which goes straight to TCP
        with the cached UUID instead of doing a zeroconf browse), in parallel.

        A dead/unreachable device gets a short ``cc.wait`` timeout
        (``_CACHE_FAST_CONNECT_TIMEOUT_S``) and is dropped with a warning.
        Healthy devices typically connect in ~1 s, so even with one offline
        device the cache-fast warm start completes in 2–3 s instead of the
        20+ s the mDNS path takes.
        """

        def _connect_one(
            host: str,
            port: int,
            friendly: str | None,
            uid_str: str,
            model_name: str | None,
        ) -> AndroidTvSwitchDevice | None:
            try:
                host_tuple = (host, port, uuid.UUID(uid_str), model_name, friendly)
                cc = pychromecast.get_chromecast_from_host(
                    host_tuple,
                    tries=1,
                    timeout=_CACHE_FAST_CONNECT_TIMEOUT_S,
                )
                cc.wait(timeout=_CACHE_FAST_CONNECT_TIMEOUT_S)
            except BaseException as ex:
                _LOGGER.warning(
                    "Cast: skipped cached %s (%s:%d) — %s",
                    friendly or uid_str,
                    host,
                    port,
                    ex,
                )
                return None
            label = (friendly or "").strip() or uid_str
            host_tuple = (host, port, uuid.UUID(uid_str), model_name, friendly)
            dev = AndroidTvSwitchDevice(
                uid_str,
                cc,
                connect_timeout=self._connection_timeout,
                display_name=label,
                host_connect_tuple=host_tuple,
            )
            try:
                dev._cast.media_controller.update_status()
                time.sleep(_STATUS_POLL_SLEEP_S)
            except Exception:
                pass
            st = dev._cast.media_controller.status
            dev.set_power(bool(st.player_is_playing))
            return dev

        results = await asyncio.gather(
            *(
                asyncio.to_thread(_connect_one, h, p, fn, uid, model)
                for h, p, fn, uid, model in known
                if uid  # safe-guard — caller already filtered, but be explicit
            ),
            return_exceptions=False,
        )
        return [d for d in results if d is not None]

    async def fetch(self, *, full_zeroconf: bool = False) -> None:
        if self._alias_to_device is not None:
            raise AlreadyInitializedError

        # ── Cache fast path: no mDNS, parallel TCP connect ───────────────
        # Triggered when every cached row carries a non-empty UUID. The
        # caller can opt out with ``full_zeroconf=True`` (used by
        # :meth:`rediscover`) which forces the LAN-wide browse below.
        if not full_zeroconf and self._discovery_store_path is not None:
            known = device_discovery_store.load_androidtv_known_devices(self._discovery_store_path)
            if known and all(uid for _, _, _, uid, _ in known):
                uniq = await self._connect_devices_from_cache(known)
                self._finalize_devices(uniq)
                self._last_discovery_source = "cache"
                # Refresh the cache so updated friendly names / model names
                # (set via display-name overrides) make it back to disk.
                self._persist_after_fetch(uniq)
                return

        # ── Targeted or full mDNS path (rewrites the cache w/ UUIDs) ─────
        mode, hints = self._discovery_browse_known_hosts(full_zeroconf=full_zeroconf)
        if mode == "hints_only" and not hints:
            self._finalize_devices([])
            # Nothing to do and no LAN sweep performed — treat as cache.
            self._last_discovery_source = "cache"
            return

        discover_hosts_arg: list[str] | None = None if mode == "full" else hints
        # Whether ``hints_only`` (targeted mDNS against cached hosts) or
        # ``full`` (LAN-wide multicast browse), we ran mDNS — so the user
        # sees this as a real discovery cycle, not a cache hit.
        self._last_discovery_source = "discovery"

        # Capture per-info metadata so we can rewrite the cache with the
        # UUID + model_name needed for the no-mDNS fast path on the next
        # startup. ``connect_all`` returns ``(device, model_name | None)``
        # pairs keyed by host:port.
        def connect_all() -> list[tuple[AndroidTvSwitchDevice, str | None]]:
            infos, browser = _discover_cast_infos_sync(
                timeout=self._zeroconf_timeout,
                known_hosts=discover_hosts_arg,
            )
            devices: list[tuple[AndroidTvSwitchDevice, str | None]] = []
            try:
                for info in infos:
                    try:
                        cc = pychromecast.get_chromecast_from_cast_info(
                            info,
                            browser.zc,
                            tries=1,
                            timeout=self._connection_timeout,
                        )
                        cc.wait(timeout=self._connection_timeout)
                        uid = str(info.uuid)
                        label = (info.friendly_name or "").strip() or uid
                        port = int(info.port) if info.port else _DEFAULT_CAST_PORT
                        host_tuple = (
                            str(info.host).strip(),
                            port,
                            uuid.UUID(uid),
                            (getattr(info, "model_name", None) or "").strip() or None,
                            (info.friendly_name or "").strip() or None,
                        )
                        dev = AndroidTvSwitchDevice(
                            uid,
                            cc,
                            connect_timeout=self._connection_timeout,
                            display_name=label,
                            host_connect_tuple=host_tuple,
                        )
                        try:
                            dev._cast.media_controller.update_status()
                            time.sleep(_STATUS_POLL_SLEEP_S)
                        except Exception:
                            pass
                        st = dev._cast.media_controller.status
                        dev.set_power(bool(st.player_is_playing))
                        model = (getattr(info, "model_name", None) or "").strip() or None
                        devices.append((dev, model))
                    except BaseException as ex:
                        _LOGGER.warning(
                            "Cast: skipped %s (%s)",
                            getattr(info, "friendly_name", None) or info.uuid,
                            ex,
                        )
                return devices
            finally:
                with contextlib.suppress(Exception):
                    browser.stop_discovery()

        pairs = await asyncio.to_thread(connect_all)
        uniq = [dev for dev, _ in pairs]
        self._finalize_devices(uniq)
        self._persist_after_fetch(uniq, model_by_uid={d.identifier: m for d, m in pairs})

    def _persist_after_fetch(
        self,
        devices: list[AndroidTvSwitchDevice],
        *,
        model_by_uid: dict[str, str | None] | None = None,
    ) -> None:
        """Rewrite ``androidtv_discovered_hosts`` with full metadata."""

        if self._discovery_store_path is None or not devices:
            return
        rows: list[tuple[str, int, str | None, str | None, str | None]] = []
        for d in devices:
            host = str(d._cast.socket_client.host)
            port = int(d._cast.socket_client.port)
            pl = d.preferred_label
            friendly = pl if pl != d.identifier else None
            uid = d.identifier
            model = (model_by_uid or {}).get(uid)
            rows.append((host, port, friendly, uid, model))
        rows.sort(key=lambda t: (t[0], t[1]))
        device_discovery_store.save_androidtv_hosts(self._discovery_store_path, rows)

    def get_device_by_alias(self, identifier: str) -> AndroidTvSwitchDevice | None:
        if self._alias_to_device is None:
            raise NotInitializedError
        return self._alias_to_device.get(identifier)

    async def is_off(self, identifier: str) -> bool:
        return not await self.is_on(identifier)

    async def is_on(self, identifier: str) -> bool:
        dev = self._device_for(identifier)
        await dev.refresh_power_state()
        return dev.is_on

    @property
    def last_discovery_source(self) -> str | None:
        """``"cache"`` (targeted mDNS against cached hosts) or ``"discovery"``
        (LAN-wide zeroconf browse) after :meth:`fetch`; ``None`` before.

        The CLI bootstrap reads this to annotate each backend's "ready" line
        with where the device list came from.
        """

        return self._last_discovery_source

    def rebuild_lookup_after_display_change(self) -> None:
        if self._alias_to_device is None:
            raise NotInitializedError
        uniq = list({id(d): d for d in self._alias_to_device.values()}.values())
        self._alias_to_device = self._expand_lookup(uniq)

    async def rediscover(self) -> None:
        await self.disconnect()
        await self.fetch(full_zeroconf=True)

    async def reload_from_cache(self) -> bool:
        """Replace the in-memory Cast map from SQLite only (never mDNS/zeroconf).

        Requires every cached row to have a UUID. On any connect miss, keeps the
        prior map. Does not rewrite the discovery table.
        """

        if self._discovery_store_path is None:
            _LOGGER.debug("AndroidTV reload_from_cache: no discovery cache path")
            return False
        if self._alias_to_device is None:
            _LOGGER.debug("AndroidTV reload_from_cache: manager not initialized")
            return False
        known = device_discovery_store.load_androidtv_known_devices(self._discovery_store_path)
        if not known or not all(uid for _h, _p, _fn, uid, _m in known):
            _LOGGER.info("AndroidTV reload_from_cache: empty or incomplete cache; keeping prior device map")
            return False
        previous = self._alias_to_device
        uniq = await self._connect_devices_from_cache(known)
        expected = sum(1 for _h, _p, _fn, uid, _m in known if uid)
        if len(uniq) != expected:
            for dev in uniq:
                cast = dev._cast
                if cast is None:
                    continue

                def _dc(c: Chromecast = cast) -> None:
                    with contextlib.suppress(Exception):
                        c.disconnect(timeout=3.0)

                await asyncio.to_thread(_dc)
            _LOGGER.warning(
                "AndroidTV reload_from_cache: reconnect incomplete (%d/%d); keeping prior device map",
                len(uniq),
                expected,
            )
            return False
        self._alias_to_device = previous
        await self.disconnect()
        self._finalize_devices(uniq)
        self._last_discovery_source = "cache"
        _LOGGER.info(
            "AndroidTV reload_from_cache: replaced device map from cache (%d device(s))",
            len(uniq),
        )
        return True

    @property
    def switches(self) -> tuple[AndroidTvSwitchDevice, ...]:
        if self._alias_to_device is None:
            raise NotInitializedError
        uniq = list({id(d): d for d in self._alias_to_device.values()}.values())
        uniq.sort(key=lambda d: d.preferred_label.lower())
        return tuple(uniq)

    async def turn_off(self, identifier: str) -> None:
        await self._device_for(identifier).turn_off()

    async def turn_on(self, identifier: str) -> None:
        await self._device_for(identifier).turn_on()

    @property
    def zeroconf_discovery(self) -> bool:
        return self._zeroconf_discovery

    @staticmethod
    def zeroconf_discovery_wanted(*, cli_opt_out: bool = False) -> bool:
        """Whether :meth:`fetch` may run an open-ended Cast browse.

        **Default is on.** ``--no-androidtv-zeroconf`` disables it; ``ANDROIDTV_ZEROCONF=0`` /
        ``false`` / ``no`` / ``off`` disables open browse (cached / explicit hosts only).
        """

        if cli_opt_out:
            return False
        v = (os.environ.get("ANDROIDTV_ZEROCONF") or "").strip().lower()
        if v in ("0", "false", "no", "off"):
            return False
        return True

    @property
    def zeroconf_timeout(self) -> float:
        return self._zeroconf_timeout
