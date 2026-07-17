"""Sonos zone control via SoCo (UPnP over the LAN).

Compatible with **S1-era and newer** households that expose the classic Sonos SOAP API on the
local network (the same stack SoCo targets). Discovery uses UDP; playback calls are run in a
thread pool so async callers are not blocked.

Cache-first startup mirrors :mod:`app.kasa_device_manager`: when a SQLite
``discovery_cache_path`` is supplied, :meth:`SonosDeviceManager.fetch` reconnects
each cached zone by host and verifies ``SoCo.uid`` matches the cached UUID
before trusting it. The UDP discovery path runs only when the cache is empty,
the user passes ``force_discovery=True``, or any cached zone fails to reconnect
/ returns an unexpected UID (e.g. a Sonos rebind after DHCP churn).

Requires the optional ``soco`` dependency (see ``pyproject.toml``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from soco import SoCo
from soco import discover as soco_discover
from soco.exceptions import SoCoUPnPException

from app import device_discovery_store
from app.device_label_conflicts import note_display_name_rename, record_duplicate_preferred_labels
from app.device_mac import lookup_mac_via_arp, mac_from_sonos_rincon
from app.device_manager import AlreadyInitializedError, NotInitializedError, SpeakerDeviceManager
from app.rule_engine import SpeakerDevice
from app.sonos_stream_favorites import SonosStreamFavorite, load_sonos_stream_favorites

_LOGGER = logging.getLogger(__name__)

_SONOS_TRANSPORT_LABELS: dict[str, str] = {
    "PAUSED_PLAYBACK": "paused",
    "PLAYING": "playing",
    "STOPPED": "stopped",
    "TRANSITIONING": "transitioning",
}

# UPnP fault code Sonos returns when the requested transport transition
# cannot proceed from the *current* state. Common triggers: ``Play`` on
# a zone with an empty queue / no media source, ``Pause`` on a zone
# that has already drifted out of ``PLAYING`` between our last poll and
# the click, or any action on a zone that is mid-``TRANSITIONING``.
# The code itself is a string in :mod:`soco` (see ``SoCoUPnPException``
# docstring) — keep the comparison stringly-typed and defensive.
_SONOS_UPNP_TRANSITION_UNAVAILABLE = "701"


class SonosTransitionUnavailableError(Exception):
    """Raised when Sonos refuses a play/pause because it can't transition.

    Wraps :class:`soco.exceptions.SoCoUPnPException` with fault code
    701 ("Transition not available") so the HTTP layer can surface a
    helpful 409 rather than a generic 500, and so the bulk-pause helper
    can skip the offending zone without taking the whole batch down.
    The original exception is available via ``__cause__`` for logging.
    """


class SonosSpeakerDevice(SpeakerDevice):
    __slots__ = ("_is_playing", "_mac_address", "_rincon_uid", "_soco", "_stream_favorites")

    def __init__(
        self,
        identifier: str,
        soco_zone: Any,
        *,
        display_name: str | None = None,
        mac_address: str,
        rincon_uid: str | None = None,
        stream_favorites: tuple[SonosStreamFavorite, ...] = (),
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._soco = soco_zone
        self._mac_address = mac_address
        self._rincon_uid = (rincon_uid or identifier).strip()
        self._stream_favorites = stream_favorites
        # Tri-state cache: ``True``/``False`` once we've successfully read
        # the transport state at least once, ``None`` while we still don't
        # know. The UI maps the three values to ``playing`` / ``paused`` /
        # ``unknown`` state badges. Live UPnP traffic only happens in
        # :meth:`update_playback_state`; everything else reads this cache.
        self._is_playing: bool | None = None

    @property
    def host(self) -> str:
        return (getattr(self._soco, "ip_address", None) or "").strip()

    @property
    def is_playing(self) -> bool | None:
        """Last observed playback state, or ``None`` before the first poll.

        Driven by :meth:`update_playback_state` (background watcher) and
        by :meth:`pause` / :meth:`resume` (immediate post-action sync).
        """

        return self._is_playing

    @property
    def mac_address(self) -> str:
        return self._mac_address

    @property
    def rincon_uid(self) -> str:
        return self._rincon_uid

    async def pause(self) -> None:
        try:
            await asyncio.to_thread(self._soco.pause)
        except SoCoUPnPException as exc:
            if str(getattr(exc, "error_code", "")) == _SONOS_UPNP_TRANSITION_UNAVAILABLE:
                # Sonos refused the pause — typically because the zone
                # has already drifted out of PLAYING (queue ended,
                # someone hit pause on the phone app between our last
                # poll and this click). Refresh from a live UPnP read
                # so the cache reflects truth, then surface a domain
                # error the endpoint maps to 409.
                await self.update_playback_state()
                raise SonosTransitionUnavailableError(
                    f"Sonos zone {self.preferred_label!r} cannot pause from "
                    f"its current transport state (likely already paused / stopped)."
                ) from exc
            raise
        # Post-action sync. Sonos sometimes lingers in ``TRANSITIONING`` for
        # a beat right after a pause, so trusting the action's commanded
        # state is more accurate than a re-poll racing the transition.
        self._is_playing = False

    async def resume(self, *, favorite_index: int = 0) -> None:
        favorite: SonosStreamFavorite | None = None
        if self._stream_favorites:
            if favorite_index < 0 or favorite_index >= len(self._stream_favorites):
                raise ValueError(
                    f"Expected favorite_index in 0..{len(self._stream_favorites) - 1}, got {favorite_index}"
                )
            favorite = self._stream_favorites[favorite_index]
        if favorite is not None:
            try:
                await asyncio.to_thread(
                    self._soco.play_uri,
                    favorite.uri,
                    title=favorite.name,
                    force_radio=True,
                )
            except SoCoUPnPException as exc:
                if str(getattr(exc, "error_code", "")) == _SONOS_UPNP_TRANSITION_UNAVAILABLE:
                    await self.update_playback_state()
                    raise SonosTransitionUnavailableError(
                        f"Sonos zone {self.preferred_label!r} cannot resume "
                        f"{favorite.name!r} — the stream may be unavailable or the "
                        f"zone is mid-transition."
                    ) from exc
                raise
            self._is_playing = True
            return
        try:
            await asyncio.to_thread(self._soco.play)
        except SoCoUPnPException as exc:
            if str(getattr(exc, "error_code", "")) == _SONOS_UPNP_TRANSITION_UNAVAILABLE:
                # Most common trigger here is an empty queue: Sonos has
                # nothing to play, so ``Play`` is rejected with UPnP
                # 701. Less common: the zone is mid-TRANSITIONING.
                # Refresh the cache from UPnP so the tile shows the
                # zone's real state, then raise a domain error the
                # endpoint maps to 409.
                await self.update_playback_state()
                raise SonosTransitionUnavailableError(
                    f"Sonos zone {self.preferred_label!r} has nothing to resume. "
                    f"Configure a stream favorite in domesti-bot.config.json or start "
                    f"playback from the Sonos app first."
                ) from exc
            raise
        self._is_playing = True

    @property
    def stream_favorites(self) -> tuple[SonosStreamFavorite, ...]:
        """Configured radio streams for this zone (from ``domesti-bot.config.json``)."""

        return self._stream_favorites

    def transport_state_summary(self) -> str:
        """Best-effort playback view from UPnP AV transport (``playing`` / ``paused`` / …).

        Live UPnP call — refreshes the cache as a side effect so callers
        that want the cheap cached value can read :attr:`is_playing`
        afterwards. The summary string preserves nuance the cache can't
        (``transitioning``, ``stopped``), useful for the REPL.
        """

        try:
            info = self._soco.get_current_transport_info()
            raw = (info or {}).get("current_transport_state") or ""
            raw = str(raw).strip()
        except Exception:
            return "unknown"
        if not raw:
            return "unknown"
        label = _SONOS_TRANSPORT_LABELS.get(raw, raw.replace("_", " ").lower())
        # Keep :attr:`is_playing` in sync so a manual REPL read of
        # ``transport_state_summary`` and the next UI poll converge on
        # the same answer.
        if label == "playing":
            self._is_playing = True
        elif label in ("paused", "stopped"):
            self._is_playing = False
        return label

    async def update_playback_state(self) -> None:
        """Refresh :attr:`is_playing` from a live UPnP transport read.

        Used by :class:`app.device_state_watcher.SonosPollingWatcher` and
        any code that needs to know "is this zone currently making
        noise" without committing to the verbose label. Failures are
        swallowed (logged by the caller); the cache keeps its last
        known value so transient LAN blips don't flicker the tile.
        """

        # ``transport_state_summary`` already runs the UPnP call in the
        # event-loop thread (it's synchronous), so push it to a worker
        # to avoid blocking the loop for slow zones.
        await asyncio.to_thread(self.transport_state_summary)


class SonosDeviceManager(SpeakerDeviceManager[SonosSpeakerDevice]):
    """Discover zones with SoCo and drive *pause* / *resume* per zone.

    Pass ``discovery_cache_path`` to enable cache-first startup. Set
    ``force_discovery=True`` to bypass the cache (matches
    :class:`KasaDeviceManager`).
    """

    def __init__(
        self,
        *,
        discovery_timeout: float = 5.0,
        discovery_cache_path: Path | str | None = None,
        force_discovery: bool = False,
    ) -> None:
        self._discovery_timeout = discovery_timeout
        self._alias_to_device: dict[str, SonosSpeakerDevice] | None = None
        self._discovery_cache_path = Path(discovery_cache_path).expanduser().resolve() if discovery_cache_path else None
        self._force_discovery = bool(force_discovery)
        self._stream_favorites = load_sonos_stream_favorites()
        # Set by :meth:`fetch` to ``"cache"`` (every cached zone reconnected
        # with a matching UID, no UDP traffic) or ``"discovery"`` (full
        # ``soco_discover`` UDP sweep). ``None`` before the first ``fetch``.
        self._last_discovery_source: str | None = None

    def _device_for(self, identifier: str) -> SonosSpeakerDevice:
        if self._alias_to_device is None:
            raise NotInitializedError
        d = self._alias_to_device.get(identifier)
        if d is None:
            raise ValueError(f"Unknown Sonos zone: {identifier!r}")
        return d

    def _expand_lookup(self, devices: list[SonosSpeakerDevice]) -> dict[str, SonosSpeakerDevice]:
        alias_map: dict[str, SonosSpeakerDevice] = {}
        for sd in devices:
            alias_map[sd.identifier] = sd
            if sd.rincon_uid and sd.rincon_uid != sd.identifier:
                alias_map[sd.rincon_uid] = sd
            if sd.mac_address and sd.mac_address != sd.identifier:
                alias_map[sd.mac_address] = sd
            label = sd.preferred_label
            if label != sd.identifier:
                alias_map[label] = sd
        return alias_map

    def _finalize(self, devices: list[SonosSpeakerDevice]) -> None:
        devices.sort(key=lambda d: d.preferred_label.lower())
        record_duplicate_preferred_labels(
            backend="sonos",
            devices=[(sd.mac_address, sd.preferred_label) for sd in devices],
        )
        self._alias_to_device = self._expand_lookup(devices)

    def _persist_cache(self, devices: list[SonosSpeakerDevice]) -> None:
        if self._discovery_cache_path is None:
            return
        prior_label_by_mac: dict[str, str] = {}
        for _uid, _host, zone_name, mac in device_discovery_store.load_sonos_zones(self._discovery_cache_path):
            mac_s = (mac or "").strip().lower()
            name_s = (zone_name or "").strip()
            if mac_s and name_s:
                prior_label_by_mac[mac_s] = name_s
        rows: list[tuple[str, str, str | None, str | None]] = []
        for sd in devices:
            zone = sd._soco
            host = (getattr(zone, "ip_address", None) or "").strip()
            uid = sd.rincon_uid.strip()
            if not host or not uid:
                continue
            display = getattr(zone, "player_name", None)
            label = (str(display).strip() if display else "") or None
            note_display_name_rename(
                backend="sonos",
                mac_address=sd.mac_address,
                previous_label=prior_label_by_mac.get(sd.mac_address.lower()),
                current_label=label,
            )
            rows.append((uid, host, label, sd.mac_address))
            if uid != sd.mac_address:
                device_discovery_store.migrate_canonical_key_to_mac(
                    self._discovery_cache_path,
                    backend="sonos",
                    old_key=uid,
                    mac=sd.mac_address,
                )
        device_discovery_store.save_sonos_zones(self._discovery_cache_path, rows)

    async def _reconnect_from_cache(self) -> list[SonosSpeakerDevice] | None:
        """Return cached zones if every row reconnects with a matching UID; ``None`` otherwise."""

        if self._discovery_cache_path is None:
            return None
        cached = device_discovery_store.load_sonos_zones(self._discovery_cache_path)
        if not cached:
            return None

        def _probe_one(host: str, expected_uid: str) -> Any | None:
            try:
                zone = SoCo(host)
                # Accessing ``.uid`` triggers a UPnP fetch; if the host moved
                # or the zone vanished, this raises and we fall back to UDP.
                actual = (zone.uid or "").strip()
            except Exception as exc:
                _LOGGER.debug(
                    "Sonos cache miss: host=%s expected_uid=%s error=%s",
                    host,
                    expected_uid,
                    exc,
                )
                return None
            if actual != expected_uid:
                _LOGGER.debug(
                    "Sonos cache mismatch: host=%s expected_uid=%s actual_uid=%s",
                    host,
                    expected_uid,
                    actual,
                )
                return None
            return zone

        devices: list[SonosSpeakerDevice] = []
        for uid, host, cached_name, cached_mac in cached:
            zone = await asyncio.to_thread(_probe_one, host, uid)
            if zone is None:
                return None
            try:
                live_name = (getattr(zone, "player_name", None) or "").strip()
            except Exception:
                live_name = ""
            cached_label = (cached_name or "").strip()
            label = live_name or cached_label or uid
            speaker = self._speaker_device(uid, zone, display_name=label, cached_mac=cached_mac)
            if speaker is None:
                return None
            devices.append(speaker)
        return devices

    def _speaker_device(
        self,
        uid: str,
        zone: Any,
        *,
        display_name: str,
        cached_mac: str | None = None,
    ) -> SonosSpeakerDevice | None:
        mac = mac_from_sonos_rincon(uid) or cached_mac
        if mac is None:
            host = (getattr(zone, "ip_address", None) or "").strip()
            if host:
                mac = lookup_mac_via_arp(host)
        if mac is None:
            _LOGGER.warning(
                "Skipping Sonos zone %s — MAC address required (RINCON/ARP miss)",
                uid,
            )
            return None
        return SonosSpeakerDevice(
            mac,
            zone,
            display_name=display_name,
            mac_address=mac,
            rincon_uid=uid,
            stream_favorites=self._stream_favorites,
        )

    async def disconnect(self) -> None:
        self._alias_to_device = None

    async def fetch(self) -> None:
        if self._alias_to_device is not None:
            raise AlreadyInitializedError

        if not self._force_discovery:
            cached = await self._reconnect_from_cache()
            if cached is not None:
                self._finalize(cached)
                self._persist_cache(cached)
                self._last_discovery_source = "cache"
                return

        # soco's ``discover`` is annotated ``timeout: int``; round our float
        # timeout up to at least 1 second so sub-second values still produce a
        # real LAN sweep instead of an immediate empty result.
        timeout = max(1, int(round(self._discovery_timeout)))

        def _run_discovery() -> set[Any]:
            found = soco_discover(timeout=timeout)
            return found if found else set()

        zones = await asyncio.to_thread(_run_discovery)
        devices: list[SonosSpeakerDevice] = []
        for z in zones:
            uid = getattr(z, "uid", None) or str(id(z))
            name = (getattr(z, "player_name", None) or "").strip() or uid
            sd = self._speaker_device(uid, z, display_name=name)
            if sd is None:
                continue
            devices.append(sd)
        self._finalize(devices)
        self._persist_cache(devices)
        self._last_discovery_source = "discovery"

    def get_device_by_alias(self, identifier: str) -> SonosSpeakerDevice | None:
        if self._alias_to_device is None:
            raise NotInitializedError
        return self._alias_to_device.get(identifier)

    async def is_playing(self, identifier: str) -> bool | None:
        """Refresh and return the zone's cached playback flag.

        Mirrors :meth:`KasaDeviceManager.is_on` so the polling watcher
        (:class:`app.device_state_watcher.SonosPollingWatcher`) follows
        the same shape as the kasa one. Returns ``None`` when the
        single UPnP read failed; the zone's cached
        :attr:`SonosSpeakerDevice.is_playing` keeps its previous value
        in that case.
        """

        device = self._device_for(identifier)
        await device.update_playback_state()
        return device.is_playing

    @property
    def is_cache_warm(self) -> bool:
        """``True`` when :meth:`fetch` will skip the UDP sweep entirely.

        The bootstrap path uses this to choose a "Loading cached devices…" vs
        "Discovering devices (parallel)…" banner without committing to a full
        probe up-front.
        """

        if self._force_discovery or self._discovery_cache_path is None:
            return False
        try:
            return bool(device_discovery_store.load_sonos_zones(self._discovery_cache_path))
        except Exception:
            return False

    @property
    def last_discovery_source(self) -> str | None:
        """``"cache"`` or ``"discovery"`` after :meth:`fetch`; ``None`` before.

        The CLI bootstrap reads this to annotate each backend's "ready" line so
        users see whether the device list came from SQLite (no LAN traffic) or
        a fresh ``soco_discover`` UDP sweep.
        """

        return self._last_discovery_source

    async def pause(self, identifier: str) -> None:
        await self._device_for(identifier).pause()

    @property
    def players(self) -> tuple[SonosSpeakerDevice, ...]:
        if self._alias_to_device is None:
            raise NotInitializedError
        uniq = list({id(p): p for p in self._alias_to_device.values()}.values())
        uniq.sort(key=lambda p: p.preferred_label.lower())
        return tuple(uniq)

    async def rediscover(self) -> None:
        """Force a fresh UDP sweep, ignoring the cache; subsequent ``fetch`` calls keep using the cache."""

        await self.disconnect()
        previous = self._force_discovery
        self._force_discovery = True
        try:
            await self.fetch()
        finally:
            self._force_discovery = previous

    async def reload_from_cache(self) -> bool:
        """Replace the in-memory zone map from SQLite only (never SSDP/UDP).

        On success the lookup map is replaced and the discovery table is **not**
        rewritten (the CLI owns those writes). Returns ``False`` (keeping the
        prior map) when the cache is empty or any cached zone fails to reconnect.
        """

        if self._discovery_cache_path is None:
            _LOGGER.debug("Sonos reload_from_cache: no discovery cache path")
            return False
        if self._alias_to_device is None:
            _LOGGER.debug("Sonos reload_from_cache: manager not initialized")
            return False
        cached = device_discovery_store.load_sonos_zones(self._discovery_cache_path)
        if not cached:
            _LOGGER.info("Sonos reload_from_cache: empty cache; keeping prior device map")
            return False
        devices = await self._reconnect_from_cache()
        if devices is None:
            _LOGGER.warning("Sonos reload_from_cache: reconnect failed; keeping prior device map")
            return False
        self._finalize(devices)
        self._last_discovery_source = "cache"
        _LOGGER.info(
            "Sonos reload_from_cache: replaced device map from cache (%d zone(s))",
            len(self.players),
        )
        return True

    async def resume(self, identifier: str, *, favorite_index: int = 0) -> None:
        await self._device_for(identifier).resume(favorite_index=favorite_index)
