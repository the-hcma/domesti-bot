"""TP-Link Kasa / Tapo LAN integration via ``python-kasa``.

Discovery uses UDP broadcasts (``kasa.Discover.discover``), same as ``kasa discover``. Most
legacy plugs and switches need **no username/password** on the LAN — leave credential
arguments unset.

Broadcast discovery can race **legacy XOR** (UDP :9999) vs **KLAP** (UDP :20002): whichever
answer is processed last wins for that IP. If KLAP wins but anonymous KLAP handshake fails
while XOR still works (common on older hardware), :meth:`fetch` retries **IOT.XOR** TCP for
``IOT.SMARTPLUGSWITCH`` then ``IOT.SMARTBULB`` (via ``device_factory.connect``).

SMART discovery may advertise **HTTPS** and/or pin ``http_port=443`` while the device only
answers **plain HTTP KLAP on port 80**. We reconnect with ``https=False``, clear
``http_port``, and drop ``port_override``. ``KlapTransport`` (python-kasa) always passed an
``SSLContext`` into aiohttp even for ``http://`` URLs, which can still produce TLS connects
(e.g. port 443); we patch ``KlapTransport._get_ssl_context`` to return ``False`` when
``connection_type.https`` is false. On ``AuthenticationError``,
``_ConnectionError``, or ``TimeoutError``, we retry that LAN profile for
``SmartDevice`` instances, then XOR sweep.

Newer KLAP devices that were linked to the Kasa/Tapo cloud **require** your **account**
email and password for the LAN KLAP handshake — there is no anonymous LAN mode for
these devices. Pass ``username`` / ``password`` or ``credentials=``, or resolve via
:mod:`app.kasa_credentials`. Setting only one of username/password is an error.
Credentials are applied **per host**: UDP discovery stays anonymous; only hosts
marked ``requires_klap_auth`` (learned on first ``AuthenticationError`` and stored
in the discovery cache) receive account credentials on reconnect/update. Legacy
XOR / anonymous-KLAP devices never get credentials attached. When account
credentials are **not** configured, KLAP-auth hosts are skipped quietly (DEBUG)
and omitted from the device list unless their cached MAC still answers ARP —
those stay as unresponsive tiles. Anonymous devices continue to work. Skipped
hosts remain listed on :attr:`hosts_requiring_klap_auth` /
:attr:`skipped_auth_hosts` for Settings.
When credentials **are** configured but handshake fails, a WARNING is logged
(see :func:`_klap_auth_recovery_hint`).

Optional SQLite persistence (see :mod:`app.device_discovery_store`): pass
``discovery_cache_path`` to skip UDP discovery when every cached host reconnects.
Configs are saved without plaintext credentials; ``requires_klap_auth`` is stored
per host. Use ``force_discovery=True`` to refresh the cache from the network.

Devices are tracked **by host** (the LAN address), not by alias: users routinely
give multiple physical outlets the same name in the Kasa/Tapo app (e.g. two
``"Plug"`` or ``"Lamp"``) and an alias-keyed map silently drops all-but-one.
The lookup map registers each device under its host (always unique) plus its
alias / display name when those keys don't collide; collisions emit a WARNING
and the duplicate stays reachable by host. Existing on-disk caches that were
written under the alias-keyed dedup may be **incomplete**; pass
``--force-discovery`` (or call :meth:`rediscover`) once after upgrading to
rebuild the cache from a fresh UDP sweep.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

from kasa import Device as KDevice
from kasa import Discover
from kasa.credentials import Credentials
from kasa.deviceconfig import (
    DeviceConfig,
    DeviceConnectionParameters,
    DeviceEncryptionType,
    DeviceFamily,
)
from kasa.exceptions import (
    AuthenticationError,
    KasaException,
    UnsupportedDeviceError,
    _ConnectionError,
)

from app import device_discovery_store
from app.device_display import format_device_display
from app.device_enums import DeviceFamilyId
from app.device_label_conflicts import note_display_name_rename, record_duplicate_preferred_labels
from app.device_mac import lookup_ip_via_arp_for_mac, lookup_mac_via_arp, mac_alive_on_lan, try_normalize_mac
from app.device_manager import (
    UNRESPONSIVE_AFTER_FAILED_POLLS,
    AlreadyInitializedError,
    NotInitializedError,
    SwitchDeviceManager,
)
from app.device_presence import DevicePresenceStore, device_presence_store
from app.rule_engine import DeviceUnresponsiveError, SwitchDevice

_LOGGER = logging.getLogger(__name__)

# Upper bounds for one recovery attempt per plug and for one recovery UDP sweep, so a
# half-dead device or a quiet LAN can never wedge the recovery watcher.
_RECOVERY_ATTEMPT_TIMEOUT_S = 30.0
_RECOVERY_SWEEP_TIMEOUT_S = 60.0


def _klap_auth_recovery_hint(
    *,
    initial_exc: BaseException,
    credentials: Credentials | None,
) -> str:
    """Build the actionable suffix for the ``skipped device`` WARNING.

    Newer KLAP-encrypted Tapo/Kasa devices that were linked to the
    Kasa/Tapo cloud need the **account email + password** for the LAN
    handshake — TP-Link's protocol has no anonymous LAN mode for these
    devices. When the initial failure was an :class:`AuthenticationError`
    we know auth (not network) is the proximate cause, so we point the
    operator at ``KASA_USERNAME`` / ``KASA_PASSWORD`` (when unset) or
    flag a likely credential mismatch (when set).

    Returns an empty string for non-auth failures so the message format
    stays the same for plain network errors.
    """

    if not isinstance(initial_exc, AuthenticationError):
        return ""
    if credentials is None:
        return (
            "; this looks like a KLAP device that was linked to the "
            "Kasa/Tapo cloud — set KASA_USERNAME + KASA_PASSWORD to "
            "your account credentials and rerun with --force-discovery"
        )
    return "; the configured KASA_USERNAME / KASA_PASSWORD may be wrong for this device's KLAP handshake"


def _patch_klap_transport_plain_http_ssl() -> None:
    """Use aiohttp ``ssl=False`` for cleartext KLAP (library always built an SSLContext)."""
    from kasa.transports.klaptransport import KlapTransport

    _original = KlapTransport._get_ssl_context

    async def _get_ssl_context(self: KlapTransport):  # type: ignore[override]
        if not self._config.connection_type.https:
            return False
        return await _original(self)

    KlapTransport._get_ssl_context = _get_ssl_context  # type: ignore[method-assign]


_patch_klap_transport_plain_http_ssl()


def _plain_http_device_config(
    cfg: DeviceConfig,
    *,
    credentials: Credentials | None,
    timeout: int,
) -> DeviceConfig:
    """LAN-first SMART profile: HTTP only and default KLAP port (not discovery's :443).

    Discovery often sets ``http_port`` to 443 or ``https=True``; KLAP still expects cleartext
    on port 80 unless the hardware truly serves TLS (then user must not use this path).
    """
    new_ct = replace(cfg.connection_type, https=False, http_port=None)
    cred = credentials if credentials is not None else cfg.credentials
    return replace(
        cfg,
        connection_type=new_ct,
        credentials=cred,
        timeout=timeout,
        port_override=None,
    )


def _config_uses_klap(cfg: DeviceConfig) -> bool:
    """True when the saved/discovered profile is KLAP (not legacy XOR)."""
    return cfg.connection_type.encryption_type is DeviceEncryptionType.Klap


async def _connect_smart_plain_http(
    cfg: DeviceConfig,
    *,
    credentials: Credentials | None,
    timeout: int,
) -> KDevice:
    """Reconnect SMART transports over HTTP when discovery incorrectly preferred HTTPS."""
    return await KDevice.connect(config=_plain_http_device_config(cfg, credentials=credentials, timeout=timeout))


async def _connect_legacy_xor(
    host: str,
    *,
    timeout: int,
    credentials: Credentials | None,
) -> KDevice:
    """Open the device using TCP XOR (``IOT.XOR`` transport only).

    We only try ``IOT.SMARTPLUGSWITCH`` and ``IOT.SMARTBULB``. Other
    ``DeviceFamily`` values do not map to XOR in python-kasa:
    ``SMART.IPCAMERA`` / doorbells always use ``SslAesTransport`` (HTTPS :443)
    regardless of the encryption_type field passed in ``DeviceConfig``, so a
    broad sweep would hit TLS against non-camera IPs and mask XOR recovery.
    """
    from kasa.device_factory import connect as kasa_factory_connect

    family_order = (DeviceFamily.IotSmartPlugSwitch, DeviceFamily.IotSmartBulb)

    last_error: Exception | None = None
    for family in family_order:
        cfg = DeviceConfig(
            host=host,
            connection_type=DeviceConnectionParameters(
                family,
                DeviceEncryptionType.Xor,
            ),
            timeout=timeout,
            credentials=credentials,
        )
        try:
            return await kasa_factory_connect(config=cfg)
        except UnsupportedDeviceError:
            continue
        except Exception as ex:
            last_error = ex
            continue
    if last_error is not None:
        raise last_error
    raise AuthenticationError(
        f"No legacy XOR connection profile matched for {host} (KLAP discovery may have won the UDP race)."
    )


async def _connect_from_saved_config(
    cfg: DeviceConfig,
    *,
    credentials: Credentials | None,
    timeout: int,
    raise_auth_failure: bool = False,
    log_failure: bool = True,
) -> KDevice | None:
    """Connect using a stored ``DeviceConfig``, mirroring discovery recovery paths.

    ``log_failure=False`` demotes the "skipped device" WARNINGs to DEBUG for callers
    that retry on a timer (:meth:`KasaDeviceManager.recover_unresponsive`), so a plug
    that stays powered off does not log a warning every cycle.

    Cache reconnect (``raise_auth_failure=False``) treats any
    :class:`~kasa.exceptions.KasaException` as a per-host skip so one flaky
    device cannot abort the whole family.

    When ``raise_auth_failure`` is True (Settings credential probes):
    :class:`AuthenticationError` is re-raised as a probe auth failure; other
    :class:`~kasa.exceptions.KasaException` subclasses (timeouts, connection
    errors) propagate unchanged so the probe can distinguish auth vs reachability.
    """
    cfg = replace(cfg, timeout=timeout)
    if credentials is not None:
        cfg = replace(cfg, credentials=credentials)
    try:
        return await KDevice.connect(config=cfg)
    except KasaException as exc:
        # Includes AuthenticationError, _ConnectionError, TimeoutError, and
        # generic ``Unable to query the device`` wrappers. A single timed-out
        # or flaky cached host must not abort the whole cache reconnect.
        last_exc: BaseException = exc
        ctype = cfg.connection_type
        if ctype.device_family.value.startswith("SMART") and ctype.encryption_type is DeviceEncryptionType.Klap:
            try:
                return await _connect_smart_plain_http(
                    cfg,
                    credentials=credentials,
                    timeout=timeout,
                )
            except KasaException as ex:
                last_exc = ex
        if _config_uses_klap(cfg):
            if raise_auth_failure:
                if isinstance(exc, AuthenticationError) or isinstance(last_exc, AuthenticationError):
                    auth_exc = last_exc if isinstance(last_exc, AuthenticationError) else exc
                    raise AuthenticationError(f"KLAP authentication failed for {cfg.host}") from auth_exc
                raise last_exc
            _LOGGER.log(
                logging.WARNING if log_failure else logging.DEBUG,
                "Kasa: skipped device at %s (%s)%s",
                cfg.host,
                type(last_exc).__name__,
                _klap_auth_recovery_hint(initial_exc=exc, credentials=credentials),
            )
            return None
        try:
            return await _connect_legacy_xor(
                cfg.host,
                timeout=timeout,
                credentials=credentials,
            )
        except Exception as ex:
            _LOGGER.log(
                logging.WARNING if log_failure else logging.DEBUG,
                "Kasa: skipped device at %s (%s); recovery failed (%s)%s",
                cfg.host,
                type(last_exc).__name__,
                ex,
                _klap_auth_recovery_hint(initial_exc=exc, credentials=credentials),
            )
            return None


class KasaDevice(SwitchDevice):
    __slots__ = ("_host", "_kDevice", "_mac_address")

    def __init__(
        self,
        identifier: str,
        kDevice: KDevice | None,
        *,
        display_name: str | None = None,
        host: str | None = None,
        mac_address: str,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._kDevice = kDevice
        self._mac_address = mac_address
        if kDevice is not None:
            self.set_power(kDevice.is_on)
            self._host = (kDevice.host or "").strip()
        else:
            self._host = (host or "").strip()
            self.set_unresponsive(True)

    @property
    def host(self) -> str:
        if self._kDevice is not None:
            return (self._kDevice.host or "").strip()
        return self._host

    def kasa_protocol_device(self) -> KDevice | None:
        """Return the python-kasa session, or ``None`` when the tile is ARP-only."""

        return self._kDevice

    @property
    def mac_address(self) -> str:
        return self._mac_address

    def require_kasa_protocol_device(self) -> KDevice:
        """Return the python-kasa session, or raise when the tile is ARP-only."""

        backend = self._kDevice
        if backend is None:
            raise DeviceUnresponsiveError(
                f"Expected a reachable Kasa device for {self.identifier}, got unresponsive LAN neighbor"
            )
        return backend

    async def turn_off(self) -> None:
        backend = self.require_kasa_protocol_device()
        await backend.turn_off()
        # Trust the commanded state. python-kasa often leaves ``is_on`` stale
        # until the next ``update()``; Vizio/Sonos/Tailwind pin cache the same way.
        self.set_power(False)
        self.set_unresponsive(False)

    async def turn_on(self) -> None:
        backend = self.require_kasa_protocol_device()
        await backend.turn_on()
        self.set_power(True)
        self.set_unresponsive(False)


def _kasa_mac_from_device(dev: KDevice) -> str | None:
    """Prefer vendor-reported MAC address (no ARP — callers layer cache / ARP)."""
    raw_mac = getattr(dev, "mac", None)
    if raw_mac:
        mac = try_normalize_mac(str(raw_mac))
        if mac is not None:
            return mac
    sys_info = getattr(dev, "sys_info", None)
    if isinstance(sys_info, dict):
        for key in ("mac", "mic_mac", "ethernet_mac"):
            raw = sys_info.get(key)
            if raw:
                mac = try_normalize_mac(str(raw))
                if mac is not None:
                    return mac
    return None


def _make_kasa_device(dev: KDevice, *, cached_mac: str | None = None) -> KasaDevice | None:
    """Build a :class:`KasaDevice` with MAC-address primary ``identifier``.

    Returns ``None`` when neither the vendor stack nor ARP can supply a MAC
    address — discovery must not admit host-only identity.
    """
    host = (dev.host or "").strip()
    mac = _kasa_mac_from_device(dev) or try_normalize_mac(cached_mac or "")
    if mac is None and host:
        mac = lookup_mac_via_arp(host)
    if mac is None:
        _LOGGER.warning(
            "Skipping Kasa device at %s — MAC address required (vendor/ARP miss)",
            host or "<unknown>",
        )
        return None
    alias = (getattr(dev, "alias", None) or "").strip() or None
    display = alias if alias and alias != mac else None
    return KasaDevice(mac, dev, display_name=display, mac_address=mac)


class KasaDeviceManager(SwitchDeviceManager[KasaDevice]):
    def __init__(
        self,
        *,
        discovery_target: str | None = None,
        discovery_timeout: int = 5,
        credentials: Credentials | None = None,
        username: str | None = None,
        password: str | None = None,
        query_timeout: int | None = None,
        discovery_cache_path: Path | str | None = None,
        force_discovery: bool = False,
        presence_store: DevicePresenceStore | None = None,
    ) -> None:
        self._device_name_to_device: dict[str, KasaDevice] | None = None
        # Consecutive failed state polls per MAC; see :data:`UNRESPONSIVE_AFTER_FAILED_POLLS`.
        self._failed_polls: dict[str, int] = {}
        self._presence = presence_store if presence_store is not None else device_presence_store
        self._discovery_target = (discovery_target or "").strip() or None
        self._discovery_timeout = discovery_timeout
        self._query_timeout = query_timeout
        self._discovery_cache_path = Path(discovery_cache_path).expanduser().resolve() if discovery_cache_path else None
        self._force_discovery = force_discovery
        # Number of roster replacements in flight (:meth:`rediscover`, :meth:`reload_from_cache`);
        # recovery stands down while it is above zero. A count, not a flag, so overlapping
        # operations cannot clear each other's guard.
        self._roster_replacements = 0
        # Set by :meth:`_fetch_impl` to ``"cache"`` (every saved config
        # reconnected without falling back to UDP) or ``"discovery"`` (full
        # ``Discover.discover`` broadcast sweep). ``None`` before first fetch.
        self._last_discovery_source: str | None = None
        # Hosts skipped during the most recent ``_fetch_impl`` because the
        # initial failure was an :class:`AuthenticationError` and every
        # recovery path exhausted. Read via :attr:`skipped_auth_hosts`.
        self._last_skipped_auth_hosts: list[str] = []
        # Hosts that need account credentials for KLAP (persisted in the
        # discovery cache). Anonymous LAN devices are never listed here.
        self._hosts_requiring_klap_auth: set[str] = set()
        # Config snapshots for KLAP-auth hosts skipped this fetch (so we can
        # persist requires_klap_auth even when the device never connected).
        self._skipped_klap_auth_configs: dict[str, tuple[str | None, dict[str, Any]]] = {}
        if self._discovery_cache_path is not None:
            for (
                host,
                _alias,
                _cfg,
                requires_klap_auth,
                _mac,
            ) in device_discovery_store.load_cached_configs(
                self._discovery_cache_path,
            ):
                if requires_klap_auth:
                    self._hosts_requiring_klap_auth.add(host)

        has_u = bool((username or "").strip())
        has_p = bool((password or "").strip())
        if has_u ^ has_p:
            raise ValueError(
                "Incomplete Kasa credentials: pass both username and password, "
                "or neither for unauthenticated LAN discovery"
            )
        if credentials is not None and (has_u or has_p):
            raise ValueError("Pass either credentials= or username/password=, not both")
        if credentials is not None:
            self._discovery_credentials = credentials
        elif has_u and has_p:
            self._discovery_credentials = Credentials(
                username=(username or "").strip(),
                password=(password or "").strip(),
            )
        else:
            self._discovery_credentials = None

    @staticmethod
    def credentials_from_env() -> Credentials | None:
        """Return credentials only when **both** ``KASA_USERNAME`` and ``KASA_PASSWORD`` are set."""
        un = (os.environ.get("KASA_USERNAME") or "").strip()
        pw = (os.environ.get("KASA_PASSWORD") or "").strip()
        if un and pw:
            return Credentials(username=un, password=pw)
        return None

    def __str__(self) -> str:
        if self._device_name_to_device is None:
            return "KasaDeviceManager(not initialized)"
        lines = ["KasaDeviceManager:"]
        uniq = list({id(kd): kd for kd in self._device_name_to_device.values()}.values())
        uniq.sort(key=lambda d: d.preferred_label.lower())
        for kd in uniq:
            lines.append(f"  {kd.preferred_label}: {kd.power_state}")
        return "\n".join(lines)

    def _device_for(self, identifier: str) -> KasaDevice:
        if self._device_name_to_device is None:
            raise NotInitializedError
        d = self._device_name_to_device.get(identifier)
        if d is None:
            raise ValueError(f"Unknown device: {identifier!r}")
        return d

    def clear_credentials(self) -> None:
        """Drop in-memory account credentials (next fetch uses anonymous LAN only)."""
        self._discovery_credentials = None

    async def disconnect(self, *, clear_map: bool = True) -> None:
        """Close TP-Link sessions and optionally clear cached devices.

        ``clear_map=False`` keeps the lookup map populated so UI polls and state
        watchers stay usable while :meth:`rediscover` runs a long UDP sweep.

        Clears the alias map even if closing a device fails, so the manager never stays
        half-initialized.
        """
        if self._device_name_to_device is None:
            return
        cached = self._device_name_to_device
        if clear_map:
            self._device_name_to_device = None
        seen: set[int] = set()
        for kd in cached.values():
            dev = kd._kDevice
            if dev is None:
                continue
            did = id(dev)
            if did in seen:
                continue
            seen.add(did)
            with contextlib.suppress(Exception):
                await dev.disconnect()

    async def _ingest_discovered_device(
        self,
        dev: KDevice,
        qtimeout: int,
        *,
        prefer_credentials: bool | None = None,
    ) -> KDevice | None:
        """Run ``update()`` with SMART plain-HTTP / XOR recovery.

        Credentials are only attached for hosts known (or discovered) to need
        KLAP account auth — anonymous devices stay credential-free.
        """
        host = (dev.host or "").strip()
        known_needs_auth = host in self._hosts_requiring_klap_auth
        use_creds = prefer_credentials if prefer_credentials is not None else known_needs_auth
        creds_order: list[Credentials | None]
        if use_creds and self._discovery_credentials is not None:
            creds_order = [self._discovery_credentials]
        elif self._discovery_credentials is not None:
            # Unknown host: try anonymous first, then account credentials.
            creds_order = [None, self._discovery_credentials]
        else:
            creds_order = [None]

        auth_failure = False
        for index, creds in enumerate(creds_order):
            is_last = index == len(creds_order) - 1
            result, was_auth_failure = await self._try_ingest_with_credentials(
                dev,
                qtimeout,
                credentials=creds,
                log_failure=is_last,
                # Suppress WARNING only for anonymous AuthenticationError when
                # no account credentials are configured; connectivity failures
                # still log at WARNING.
                quiet_anonymous_auth=self._discovery_credentials is None,
            )
            if result is not None:
                if creds is not None:
                    self._hosts_requiring_klap_auth.add(host)
                else:
                    self._hosts_requiring_klap_auth.discard(host)
                self._skipped_klap_auth_configs.pop(host, None)
                return result
            # Keep any AuthenticationError from an earlier attempt (e.g. anonymous
            # KLAP reject) even when a later credential retry fails for a
            # non-auth reason such as a timeout.
            auth_failure = auth_failure or was_auth_failure
        # Known KLAP hosts must be tracked even when the credential attempt
        # fails for a non-auth reason (timeout, etc.) — same as cache reconnect.
        if host and (auth_failure or known_needs_auth):
            if host not in self._last_skipped_auth_hosts:
                self._last_skipped_auth_hosts.append(host)
            self._hosts_requiring_klap_auth.add(host)
            try:
                cfg_dict = dev.config.to_dict_control_credentials(
                    exclude_credentials=True,
                )
            except Exception:
                cfg_dict = {"host": host}
            self._skipped_klap_auth_configs[host] = (getattr(dev, "alias", None), cfg_dict)
            if self._discovery_credentials is None:
                _LOGGER.debug(
                    "Kasa: ignoring KLAP-auth host %s (no account credentials configured)",
                    host,
                )
        return None

    def _announce_responsive(self, kd: KasaDevice) -> None:
        notice = self._presence.note_reappeared(
            device_id=kd.mac_address,
            family_id=DeviceFamilyId.KASA.value,
            host=kd.host or None,
            label=kd.preferred_label,
        )
        if notice is not None:
            _LOGGER.info("Kasa: %s is responding again", format_device_display(kd.mac_address, kd.preferred_label))

    def _announce_unresponsive(self, kd: KasaDevice) -> None:
        notice = self._presence.note_disappeared(
            device_id=kd.mac_address,
            family_id=DeviceFamilyId.KASA.value,
            host=kd.host or None,
            label=kd.preferred_label,
        )
        if notice is not None:
            _LOGGER.warning("Kasa: %s stopped responding", format_device_display(kd.mac_address, kd.preferred_label))

    async def _keep_arp_visible_cached_switch(
        self,
        devices_by_host: dict[str, KasaDevice],
        *,
        alias: str | None,
        cached_mac: str | None,
        host: str,
        keep_offline: bool = False,
    ) -> bool:
        """Keep a protocol-silent cached MAC as an unresponsive tile when ARP still sees it.

        ``keep_offline=True`` also keeps it at its cached address when ARP does not see it
        either: KLAP rows are skipped without a full sweep, so this is their only chance to
        stay on the dashboard while the plug is powered off (domesti-bot#867).

        Returns True when the host was retained (or already present under the ARP IP).
        """

        mac = try_normalize_mac(cached_mac or "")
        if mac is None:
            return False
        on_lan = await asyncio.to_thread(mac_alive_on_lan, mac=mac, host=host)
        if not on_lan and not keep_offline:
            return False
        arp_ip = (await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac) if on_lan else None) or host
        if arp_ip in devices_by_host:
            return True
        devices_by_host[arp_ip] = KasaDevice(
            mac,
            None,
            display_name=alias,
            host=arp_ip,
            mac_address=mac,
        )
        _LOGGER.info(
            "Kasa: %s is %s but not answering the protocol; keeping as unresponsive",
            format_device_display(mac, alias),
            "on the LAN (ARP)" if on_lan else "not on the LAN (powered off?)",
        )
        return True

    def _persist_discovery_cache(
        self,
        alias_map: dict[str, KasaDevice],
        *,
        alias_overrides: dict[str, str] | None = None,
        merge_prior_klap_hosts: bool = True,
        record_renames: bool = True,
    ) -> None:
        if self._discovery_cache_path is None:
            return
        # Preserve KLAP-auth hosts that were skipped (e.g. no credentials) so a
        # successful anonymous-only cache write does not forget them.
        prior_by_host = {
            host: (alias, cfg_dict, requires_klap_auth, mac)
            for host, alias, cfg_dict, requires_klap_auth, mac in (
                device_discovery_store.load_cached_configs(self._discovery_cache_path)
            )
        }
        prior_label_by_mac: dict[str, str] = {}
        for alias, _cfg, _requires, mac in prior_by_host.values():
            mac_s = (mac or "").strip().lower()
            alias_s = (alias or "").strip()
            if mac_s and alias_s:
                prior_label_by_mac[mac_s] = alias_s
        rows: list[tuple[str, str | None, dict[str, Any], bool, str | None]] = []
        seen_hosts: set[str] = set()
        seen_ids: set[int] = set()
        seen_macs: set[str] = set()
        for kd in alias_map.values():
            did = id(kd)
            if did in seen_ids:
                continue
            seen_ids.add(did)
            dev = kd._kDevice
            if dev is None:
                host = kd.host
                if not host:
                    continue
                prior = None
                stub_mac = try_normalize_mac(kd.mac_address or "")
                if stub_mac is not None:
                    for _old_host, row in prior_by_host.items():
                        if try_normalize_mac(row[3] or "") == stub_mac:
                            prior = row
                            break
                if prior is None:
                    prior = prior_by_host.get(host)
                if prior is None:
                    continue
                alias, cfg_dict, requires_klap_auth, prior_mac = prior
                if cfg_dict.get("host") != host:
                    cfg_dict = {**cfg_dict, "host": host}
                rows.append(
                    (
                        host,
                        alias or kd.preferred_label,
                        cfg_dict,
                        requires_klap_auth,
                        kd.mac_address or prior_mac,
                    )
                )
                seen_hosts.add(host)
                kept_mac = (kd.mac_address or prior_mac or "").strip().lower()
                if kept_mac:
                    seen_macs.add(kept_mac)
                continue
            host = (dev.host or "").strip()
            if not host:
                continue
            cfg_dict = dev.config.to_dict_control_credentials(exclude_credentials=True)
            mac = kd.mac_address
            override = None
            if alias_overrides is not None and mac:
                override = alias_overrides.get(mac.lower())
            alias = override if override is not None else getattr(dev, "alias", None)
            if record_renames:
                note_display_name_rename(
                    backend="kasa",
                    mac_address=mac,
                    previous_label=prior_label_by_mac.get(mac.lower()),
                    current_label=(alias or "").strip() or None,
                )
            rows.append(
                (
                    host,
                    alias,
                    cfg_dict,
                    host in self._hosts_requiring_klap_auth,
                    mac,
                )
            )
            seen_hosts.add(host)
            if mac:
                seen_macs.add(mac.lower())
            for old_key in (host, (alias or "").strip()):
                if old_key and old_key != mac:
                    device_discovery_store.migrate_canonical_key_to_mac(
                        self._discovery_cache_path,
                        backend="kasa",
                        old_key=old_key,
                        mac=mac,
                    )
        for host in sorted(self._hosts_requiring_klap_auth):
            if host in seen_hosts:
                continue
            if merge_prior_klap_hosts:
                prior = prior_by_host.get(host)
                if prior is not None:
                    alias, cfg_dict, _requires, prior_mac = prior
                    if (prior_mac or "").strip().lower() in seen_macs:
                        continue
                    rows.append((host, alias, cfg_dict, True, prior_mac))
                    continue
            skipped = self._skipped_klap_auth_configs.get(host)
            if skipped is None:
                continue
            alias, cfg_dict = skipped
            prior_mac = prior_by_host.get(host, (None, None, None, None))[3]
            if (prior_mac or "").strip().lower() in seen_macs:
                continue
            rows.append((host, alias, cfg_dict, True, prior_mac))
        rows.sort(key=lambda r: r[0])
        device_discovery_store.save_configs(self._discovery_cache_path, rows)

    async def _reconnect_unresponsive(
        self,
        stub: KasaDevice,
        cached: tuple[str, dict[str, Any], bool] | None,
        qtimeout: int,
    ) -> KasaDevice | None:
        """Try the stub's last address, then the ARP address, for one plug; ``None`` when silent."""

        if cached is None:
            return None
        cached_host, cfg_dict, requires_klap_auth = cached
        creds = self._discovery_credentials if requires_klap_auth else None
        if requires_klap_auth and creds is None:
            return None
        mac = stub.mac_address
        arp_ip = await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac)
        hosts = list(dict.fromkeys(h for h in (stub.host or cached_host, arp_ip) if h))
        for host in hosts:
            try:
                kd = await asyncio.wait_for(
                    self._connect_unresponsive_at(
                        host,
                        cfg_dict,
                        mac=mac,
                        credentials=creds,
                        qtimeout=qtimeout,
                    ),
                    timeout=_RECOVERY_ATTEMPT_TIMEOUT_S,
                )
            except Exception:
                _LOGGER.debug("Kasa: recovery attempt for %s at %s failed", mac, host, exc_info=True)
                continue
            if kd is not None:
                return kd
        return None

    def _record_failed_poll(self, kd: KasaDevice) -> None:
        failures = self._failed_polls.get(kd.mac_address, 0) + 1
        self._failed_polls[kd.mac_address] = failures
        if failures >= UNRESPONSIVE_AFTER_FAILED_POLLS and not kd.unresponsive:
            kd.set_unresponsive(True)
            self._announce_unresponsive(kd)

    def _record_successful_poll(self, kd: KasaDevice) -> None:
        self._failed_polls.pop(kd.mac_address, None)
        # A control action (turn_on/turn_off) clears the flag itself, so announce the return
        # even when the flag is already down; this is a no-op unless a notice is open.
        kd.set_unresponsive(False)
        self._announce_responsive(kd)

    async def _recover_stub(
        self,
        mac: str,
        stub: KasaDevice,
        cached: tuple[str, dict[str, Any], bool] | None,
        qtimeout: int,
        recovered: dict[str, KasaDevice],
    ) -> None:
        """Retry one unresponsive plug and record its live session in ``recovered`` at once."""

        kd = await self._reconnect_unresponsive(stub, cached, qtimeout)
        if kd is not None:
            recovered[mac] = kd

    async def _reload_from_cache_unguarded(self) -> bool:
        if self._discovery_cache_path is None:
            _LOGGER.debug("Kasa reload_from_cache: no discovery cache path")
            return False
        if self._device_name_to_device is None:
            _LOGGER.debug("Kasa reload_from_cache: manager not initialized")
            return False
        cached = device_discovery_store.load_cached_configs(self._discovery_cache_path)
        if not cached:
            previous_map = self._device_name_to_device
            self._hosts_requiring_klap_auth = set()
            self._last_skipped_auth_hosts = []
            self._skipped_klap_auth_configs = {}
            self._finalize_kasa_lookup({})
            self._last_discovery_source = "cache"
            await self._disconnect_device_map(previous_map)
            _LOGGER.info("Kasa reload_from_cache: empty cache; cleared device map")
            return True

        previous_map = self._device_name_to_device
        previous_klap = set(self._hosts_requiring_klap_auth)
        previous_skipped = list(self._last_skipped_auth_hosts)
        previous_skipped_cfgs = dict(self._skipped_klap_auth_configs)

        # Clear before reconnect so hosts dropped from SQLite do not keep a
        # stale KLAP-auth marker (``_connect_devices_from_cache`` only
        # add/discards rows present in ``cached``).
        self._hosts_requiring_klap_auth = set()
        self._last_skipped_auth_hosts = []
        self._skipped_klap_auth_configs = {}

        from_cache = await self._connect_devices_from_cache(cached, keep_offline=True)
        if from_cache is None:
            self._hosts_requiring_klap_auth = previous_klap
            self._last_skipped_auth_hosts = previous_skipped
            self._skipped_klap_auth_configs = previous_skipped_cfgs
            _LOGGER.warning("Kasa reload_from_cache: reconnect failed; keeping prior device map")
            return False

        self._finalize_kasa_lookup(from_cache)
        self._last_discovery_source = "cache"
        await self._disconnect_device_map(previous_map)
        _LOGGER.info(
            "Kasa reload_from_cache: replaced device map from cache (%d switch(es))",
            len(self.switches),
        )
        return True

    @contextlib.contextmanager
    def _replacing_roster(self) -> Iterator[None]:
        """Mark a roster replacement in flight so the recovery watcher stands down (nesting-safe)."""

        self._roster_replacements += 1
        try:
            yield
        finally:
            self._roster_replacements -= 1

    async def _retain_cached_switches(
        self,
        devices_by_host: dict[str, KasaDevice],
        *,
        occupied_hosts: frozenset[str] = frozenset(),
    ) -> None:
        """Keep every cached MAC the sweep missed, as an unresponsive tile when nothing answers.

        A cached MAC that still answers ARP is reconnected at its ARP address when
        possible. A MAC that answers neither the protocol nor ARP (the plug lost power
        or Wi-Fi) is kept at its cached address: switching a plug off must not remove
        its tile or its cache row. When another device now owns that address the cache
        (keyed by host) cannot hold both, so the missing plug is logged and dropped.
        ``occupied_hosts`` are addresses that answered discovery but whose device could not
        be read (for example a KLAP handshake failure): they count as owned too, unless ARP
        shows this very MAC there.
        """

        if self._discovery_cache_path is None:
            return
        qtimeout = self._query_timeout if self._query_timeout is not None else DeviceConfig.DEFAULT_TIMEOUT
        seen_macs = {kd.mac_address for kd in devices_by_host.values() if kd.mac_address}
        seen_hosts = set(devices_by_host)
        cached = device_discovery_store.load_cached_configs(self._discovery_cache_path)
        for host, alias, cfg_dict, requires_klap_auth, cached_mac in cached:
            mac = try_normalize_mac(cached_mac or "")
            if mac is None or mac in seen_macs:
                continue
            on_lan = await asyncio.to_thread(mac_alive_on_lan, mac=mac, host=host)
            arp_ip = (await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac) if on_lan else None) or host
            # An address that answered discovery but could not be read is another device's
            # unless ARP puts this very MAC there (a KLAP plug we cannot authenticate to).
            taken_by_unread_device = arp_ip in occupied_hosts and not on_lan
            if arp_ip in seen_hosts or taken_by_unread_device:
                if not on_lan:
                    _LOGGER.warning(
                        "Kasa: %s is not answering and its address %s now belongs to another device; "
                        "cannot keep it in the discovery cache",
                        format_device_display(mac, alias),
                        arp_ip,
                    )
                continue
            if on_lan:
                creds = self._discovery_credentials if requires_klap_auth else None
                cfg = DeviceConfig.from_dict(cfg_dict)
                if arp_ip != host:
                    cfg = replace(cfg, host=arp_ip)
                dev = await _connect_from_saved_config(
                    cfg,
                    credentials=creds,
                    timeout=qtimeout,
                )
                if dev is not None:
                    kd = _make_kasa_device(dev, cached_mac=mac)
                    if kd is not None:
                        devices_by_host[dev.host] = kd
                        seen_macs.add(mac)
                        seen_hosts.add(dev.host)
                        continue
                    with contextlib.suppress(Exception):
                        await dev.disconnect()
            stub = KasaDevice(
                mac,
                None,
                display_name=alias,
                host=arp_ip,
                mac_address=mac,
            )
            devices_by_host[arp_ip] = stub
            seen_macs.add(mac)
            seen_hosts.add(arp_ip)
            if on_lan:
                _LOGGER.info(
                    "Kasa: %s is on the LAN (ARP) but not answering discovery; keeping as unresponsive",
                    format_device_display(mac, alias),
                )
            else:
                _LOGGER.info(
                    "Kasa: %s is not answering discovery or ARP (powered off?); keeping as unresponsive",
                    format_device_display(mac, alias),
                )

    async def _sweep_for_unresponsive(
        self,
        macs: set[str],
        *,
        found: dict[str, KasaDevice],
        live_hosts: set[str],
        qtimeout: int,
    ) -> None:
        """One UDP sweep that keeps only the plugs in ``macs`` (their address may have changed).

        Matches are added to ``found`` as they are read, so the caller can close them if it is cancelled.
        """

        discover_kw: dict[str, Any] = {"discovery_timeout": self._discovery_timeout, "timeout": qtimeout}
        if self._discovery_target is not None:
            discover_kw["target"] = self._discovery_target
        try:
            devices = await asyncio.wait_for(Discover.discover(**discover_kw), timeout=_RECOVERY_SWEEP_TIMEOUT_S)
        except Exception:
            _LOGGER.debug("Kasa: recovery sweep failed", exc_info=True)
            return
        for host, discovered in devices.items():
            if host in live_hosts:
                continue
            try:
                finalized = await asyncio.wait_for(
                    self._ingest_discovered_device(discovered, qtimeout),
                    timeout=_RECOVERY_ATTEMPT_TIMEOUT_S,
                )
            except BaseException as exc:
                # A timeout or a cancelled cycle can leave the half-read plug's session open.
                with contextlib.suppress(Exception):
                    await discovered.disconnect()
                if not isinstance(exc, Exception):
                    raise
                _LOGGER.debug("Kasa: recovery sweep could not read %s", host, exc_info=True)
                continue
            kd = _make_kasa_device(finalized) if finalized is not None else None
            if kd is not None and kd.mac_address in macs and kd.mac_address not in found:
                found[kd.mac_address] = kd
                continue
            if finalized is not None:
                with contextlib.suppress(Exception):
                    await finalized.disconnect()

    async def _try_ingest_with_credentials(
        self,
        dev: KDevice,
        qtimeout: int,
        *,
        credentials: Credentials | None,
        log_failure: bool,
        quiet_anonymous_auth: bool = False,
    ) -> tuple[KDevice | None, bool]:
        """Attempt update/recovery for ``dev`` using optional account credentials.

        Returns ``(device_or_none, auth_failure)`` where ``auth_failure`` is
        True when the initial failure was :class:`AuthenticationError`.
        When ``quiet_anonymous_auth`` is True, anonymous auth failures are not
        logged at WARNING (expected when no account credentials are configured).
        """
        cfg_before = dev.config
        host = dev.host
        initial_exc: BaseException | None = None

        if credentials is not None:
            cfg_before = replace(cfg_before, credentials=credentials, timeout=qtimeout)
            with contextlib.suppress(Exception):
                await dev.disconnect()
            connected: KDevice | None = None
            try:
                connected = await KDevice.connect(config=cfg_before)
                await connected.update()
                return connected, False
            except (AuthenticationError, _ConnectionError) as exc:
                initial_exc = exc
                if connected is not None:
                    with contextlib.suppress(Exception):
                        await connected.disconnect()
            except Exception as exc:
                if connected is not None:
                    with contextlib.suppress(Exception):
                        await connected.disconnect()
                if log_failure:
                    _LOGGER.warning(
                        "Kasa: skipped device at %s (%s)",
                        host,
                        type(exc).__name__,
                    )
                return None, False
        else:
            try:
                await dev.update()
                return dev, False
            except (AuthenticationError, _ConnectionError) as exc:
                initial_exc = exc
                with contextlib.suppress(Exception):
                    await dev.disconnect()
            except Exception as exc:
                if log_failure:
                    _LOGGER.warning(
                        "Kasa: skipped device at %s (%s)",
                        host,
                        type(exc).__name__,
                    )
                return None, False

        assert initial_exc is not None
        auth_failure = isinstance(initial_exc, AuthenticationError)
        last_exc: BaseException = initial_exc
        if cfg_before.connection_type.device_family.value.startswith("SMART"):
            try:
                return (
                    await _connect_smart_plain_http(
                        cfg_before,
                        credentials=credentials,
                        timeout=qtimeout,
                    ),
                    False,
                )
            except Exception as ex:
                last_exc = ex
        if _config_uses_klap(cfg_before):
            suppress_warning = quiet_anonymous_auth and auth_failure and credentials is None
            if log_failure and not suppress_warning:
                _LOGGER.warning(
                    "Kasa: skipped device at %s (%s)%s",
                    host,
                    type(last_exc).__name__,
                    _klap_auth_recovery_hint(
                        initial_exc=initial_exc,
                        credentials=credentials,
                    ),
                )
            return None, auth_failure
        try:
            return (
                await _connect_legacy_xor(
                    host,
                    timeout=qtimeout,
                    credentials=credentials,
                ),
                False,
            )
        except Exception as ex:
            suppress_warning = quiet_anonymous_auth and auth_failure and credentials is None
            if log_failure and not suppress_warning:
                _LOGGER.warning(
                    "Kasa: skipped device at %s (%s); recovery failed (%s)%s",
                    host,
                    type(last_exc).__name__,
                    ex,
                    _klap_auth_recovery_hint(
                        initial_exc=initial_exc,
                        credentials=credentials,
                    ),
                )
            return None, auth_failure

    def _expand_kasa_lookup(self, devices: list[KasaDevice]) -> dict[str, KasaDevice]:
        """Build the multi-key lookup: MAC/host (always) + alias / display name (when unique).

        The canonical UI key is the MAC address. Aliases and
        display names register as *additional* lookup keys; if two devices share
        an alias only the first claimant wins that key — the duplicate stays
        reachable via MAC address / host and a warning is logged.
        """

        lookup: dict[str, KasaDevice] = {}
        for kd in devices:
            host = kd.host
            if host:
                lookup[host] = kd
            if kd.mac_address:
                lookup[kd.mac_address] = kd
            if kd.identifier and kd.identifier not in lookup:
                lookup[kd.identifier] = kd
        for kd in devices:
            host = kd.host
            candidate_keys: list[str] = [kd.identifier]
            if kd.preferred_label != kd.identifier:
                candidate_keys.append(kd.preferred_label)
            alias = (getattr(kd._kDevice, "alias", None) or "").strip()
            if alias:
                candidate_keys.append(alias)
            display = format_device_display(kd.identifier, kd.preferred_label)
            if display:
                candidate_keys.append(display)
            for key in candidate_keys:
                if not key or key == host or key == kd.mac_address:
                    continue
                existing = lookup.get(key)
                if existing is None:
                    lookup[key] = kd
                elif existing is not kd:
                    _LOGGER.warning(
                        "Kasa: lookup key %r is shared by %s and %s — the "
                        "duplicate is reachable by host (%s) only. Rename "
                        "one of them in the Kasa/Tapo app to disambiguate.",
                        key,
                        existing.host,
                        host,
                        host,
                    )
        return lookup

    async def _connect_devices_from_cache(
        self,
        cached: list[tuple[str, str | None, dict[str, Any], bool, str | None]],
        *,
        keep_offline: bool = False,
    ) -> dict[str, KasaDevice] | None:
        """Reconnect every saved config without UDP.

        ``keep_offline=True`` keeps a plug that answers neither the protocol nor ARP as an
        unresponsive tile instead of returning ``None``. Cache-first startup leaves it off, so
        a dead host still triggers its one sweep; :meth:`reload_from_cache` turns it on, since
        it never sweeps and the CLI keeps such rows in the cache (domesti-bot#867).

        Returns a host→device map on success (including an empty map when every
        row was a skipped KLAP host). Returns ``None`` when a non-auth failure
        would historically invalidate the cache and fall through to
        ``Discover.discover`` — callers that must never broadcast should treat
        ``None`` as failure and keep their prior map.
        """

        qtimeout = self._query_timeout if self._query_timeout is not None else DeviceConfig.DEFAULT_TIMEOUT
        # Dedup by ``host`` (the LAN identifier, guaranteed unique by
        # virtue of being an IP address) rather than by ``alias`` —
        # users routinely give multiple physical outlets the same name
        # in the Kasa/Tapo app (e.g. two "Plug" or "Lamp") and the old
        # alias-keyed map silently dropped all-but-one of them.
        devices_by_host: dict[str, KasaDevice] = {}
        for host, _alias, cfg_dict, requires_klap_auth, cached_mac in cached:
            if requires_klap_auth:
                self._hosts_requiring_klap_auth.add(host)
            else:
                self._hosts_requiring_klap_auth.discard(host)
            # Known KLAP-auth host and no credentials: skip quietly and
            # keep using the cache for anonymous devices.
            if requires_klap_auth and self._discovery_credentials is None:
                self._last_skipped_auth_hosts.append(host)
                _LOGGER.debug(
                    "Kasa: ignoring KLAP-auth host %s (no account credentials configured)",
                    host,
                )
                await self._keep_arp_visible_cached_switch(
                    devices_by_host,
                    alias=_alias,
                    cached_mac=cached_mac,
                    host=host,
                    keep_offline=True,
                )
                continue
            cfg = DeviceConfig.from_dict(cfg_dict)
            # Only attach account credentials for hosts that need KLAP auth.
            creds = self._discovery_credentials if requires_klap_auth else None
            dev = await _connect_from_saved_config(
                cfg,
                credentials=creds,
                timeout=qtimeout,
            )
            if dev is None and requires_klap_auth is False:
                # Legacy row or mis-classified host: retry with credentials.
                if self._discovery_credentials is not None:
                    dev = await _connect_from_saved_config(
                        cfg,
                        credentials=self._discovery_credentials,
                        timeout=qtimeout,
                    )
                    if dev is not None:
                        self._hosts_requiring_klap_auth.add(host)
            if dev is None:
                if requires_klap_auth:
                    # Credentials present but handshake failed — track and
                    # continue with other cached hosts (do not invalidate cache).
                    self._last_skipped_auth_hosts.append(host)
                    self._hosts_requiring_klap_auth.add(host)
                    _LOGGER.warning(
                        "Kasa: skipped KLAP-auth host %s (credentials configured but handshake failed)",
                        host,
                    )
                    await self._keep_arp_visible_cached_switch(
                        devices_by_host,
                        alias=_alias,
                        cached_mac=cached_mac,
                        host=host,
                        keep_offline=True,
                    )
                    continue
                if await self._keep_arp_visible_cached_switch(
                    devices_by_host,
                    alias=_alias,
                    cached_mac=cached_mac,
                    host=host,
                    keep_offline=keep_offline,
                ):
                    continue
                for kd in devices_by_host.values():
                    backend = kd._kDevice
                    if backend is None:
                        continue
                    with contextlib.suppress(Exception):
                        await backend.disconnect()
                return None
            # Device is already connected with the right credentials (or
            # anonymously). Run update + SMART plain-HTTP / XOR recovery
            # via the same path discovery ingest uses — bare update()
            # would invalidate the whole cache on a recoverable failure.
            ingested, was_auth_failure = await self._try_ingest_with_credentials(
                dev,
                qtimeout,
                credentials=None,
                log_failure=True,
                quiet_anonymous_auth=(not requires_klap_auth and self._discovery_credentials is None),
            )
            if ingested is None and (
                not requires_klap_auth and was_auth_failure and self._discovery_credentials is not None
            ):
                # Mis-classified row: anonymous connect worked but update
                # needs KLAP account credentials.
                dev2 = await _connect_from_saved_config(
                    cfg,
                    credentials=self._discovery_credentials,
                    timeout=qtimeout,
                )
                if dev2 is not None:
                    with contextlib.suppress(Exception):
                        await dev.disconnect()
                    # Own the credentialed session for later cleanup / skip_cfg.
                    dev = dev2
                    ingested, was_auth2 = await self._try_ingest_with_credentials(
                        dev,
                        qtimeout,
                        credentials=None,
                        log_failure=True,
                        quiet_anonymous_auth=False,
                    )
                    was_auth_failure = was_auth_failure or was_auth2
                    if ingested is not None:
                        self._hosts_requiring_klap_auth.add(host)
            if ingested is not None:
                kd = _make_kasa_device(ingested, cached_mac=cached_mac)
                if kd is None:
                    with contextlib.suppress(Exception):
                        await ingested.disconnect()
                    continue
                devices_by_host[ingested.host] = kd
                continue
            if requires_klap_auth or was_auth_failure:
                # KLAP (known or newly discovered): skip this host without
                # invalidating the rest of the cache.
                if host not in self._last_skipped_auth_hosts:
                    self._last_skipped_auth_hosts.append(host)
                self._hosts_requiring_klap_auth.add(host)
                try:
                    skip_cfg = dev.config.to_dict_control_credentials(
                        exclude_credentials=True,
                    )
                except Exception:
                    skip_cfg = {"host": host}
                self._skipped_klap_auth_configs[host] = (
                    getattr(dev, "alias", None),
                    skip_cfg,
                )
                if requires_klap_auth:
                    _LOGGER.warning(
                        "Kasa: skipped KLAP-auth host %s (credentials configured but handshake failed)",
                        host,
                    )
                with contextlib.suppress(Exception):
                    await dev.disconnect()
                await self._keep_arp_visible_cached_switch(
                    devices_by_host,
                    alias=_alias,
                    cached_mac=cached_mac,
                    host=host,
                )
                continue
            with contextlib.suppress(Exception):
                await dev.disconnect()
            for kd in devices_by_host.values():
                backend = kd._kDevice
                if backend is None:
                    continue
                with contextlib.suppress(Exception):
                    await backend.disconnect()
            return None
        return devices_by_host

    async def _connect_unresponsive_at(
        self,
        host: str,
        cfg_dict: dict[str, Any],
        *,
        mac: str,
        credentials: Credentials | None,
        qtimeout: int,
    ) -> KasaDevice | None:
        """Connect to ``host`` and return a live device only when it is the plug with ``mac``."""

        cfg = replace(DeviceConfig.from_dict(cfg_dict), host=host)
        dev = await _connect_from_saved_config(
            cfg,
            credentials=credentials,
            timeout=qtimeout,
            log_failure=False,
        )
        if dev is None:
            return None
        try:
            ingested, _auth_failure = await self._try_ingest_with_credentials(
                dev,
                qtimeout,
                credentials=None,
                log_failure=False,
                quiet_anonymous_auth=True,
            )
        except BaseException:
            # Includes the attempt timeout's cancellation: never leak a half-open session.
            with contextlib.suppress(Exception):
                await dev.disconnect()
            raise
        # No cached MAC here: the plug must prove its identity (vendor MAC, else ARP).
        kd = _make_kasa_device(ingested) if ingested is not None else None
        if kd is None or kd.mac_address != mac:
            with contextlib.suppress(Exception):
                await dev.disconnect()
            if ingested is not None and ingested is not dev:
                with contextlib.suppress(Exception):
                    await ingested.disconnect()
            return None
        return kd

    async def _disconnect_device_map(
        self,
        alias_map: dict[str, KasaDevice] | None,
    ) -> None:
        """Close TP-Link sessions for every unique device in ``alias_map``."""

        if alias_map is None:
            return
        seen: set[int] = set()
        for kd in alias_map.values():
            dev = kd._kDevice
            if dev is None:
                continue
            did = id(dev)
            if did in seen:
                continue
            seen.add(did)
            with contextlib.suppress(Exception):
                await dev.disconnect()

    async def _fetch_impl(self, *, force_discovery: bool) -> None:
        qtimeout = self._query_timeout if self._query_timeout is not None else DeviceConfig.DEFAULT_TIMEOUT
        # Reset per-fetch state so subsequent rediscovers don't carry
        # stale auth-failure markers from a previous attempt that ran
        # without credentials.
        self._last_skipped_auth_hosts = []
        self._skipped_klap_auth_configs = {}

        devices_by_host: dict[str, KasaDevice] = {}

        if self._discovery_cache_path is not None and not force_discovery:
            cached = device_discovery_store.load_cached_configs(self._discovery_cache_path)
            if cached:
                from_cache = await self._connect_devices_from_cache(cached)
                if from_cache is not None:
                    self._finalize_kasa_lookup(from_cache)
                    self._persist_discovery_cache(self._device_name_to_device or {})
                    self._last_discovery_source = "cache"
                    return

        # Always discover anonymously; attach credentials per-host during ingest.
        if self._discovery_target is None and self._query_timeout is None and self._discovery_timeout == 5:
            devices = await Discover.discover()
        else:
            discover_kw: dict[str, Any] = {"discovery_timeout": self._discovery_timeout}
            if self._discovery_target is not None:
                discover_kw["target"] = self._discovery_target
            if self._query_timeout is not None:
                discover_kw["timeout"] = self._query_timeout
            devices = await Discover.discover(**discover_kw)

        discovered_count = len(devices)
        try:
            for discovered in devices.values():
                finalized = await self._ingest_discovered_device(discovered, qtimeout)
                if finalized is None:
                    continue
                kd = _make_kasa_device(finalized)
                if kd is None:
                    with contextlib.suppress(Exception):
                        await finalized.disconnect()
                    continue
                devices_by_host[finalized.host] = kd
        except BaseException:
            for kd in devices_by_host.values():
                backend = kd._kDevice
                if backend is None:
                    continue
                with contextlib.suppress(Exception):
                    await backend.disconnect()
            raise

        ingested_count = len(devices_by_host)
        if ingested_count != discovered_count:
            _LOGGER.warning(
                "Kasa: discovered %d device(s) on the LAN but only %d "
                "completed update/recovery; %d skipped (see WARNING logs above).",
                discovered_count,
                ingested_count,
                discovered_count - ingested_count,
            )
        else:
            _LOGGER.info("Kasa: discovered %d device(s)", discovered_count)

        await self._retain_cached_switches(
            devices_by_host,
            occupied_hosts=frozenset(
                {*devices, *((discovered.host or "").strip() for discovered in devices.values())} - {""}
            ),
        )

        self._finalize_kasa_lookup(devices_by_host)
        self._persist_discovery_cache(
            self._device_name_to_device or {},
            merge_prior_klap_hosts=not force_discovery,
        )
        self._last_discovery_source = "discovery"

    def _finalize_kasa_lookup(self, alias_map: dict[str, KasaDevice]) -> None:
        """Apply SQLite display names (keyed by MAC or host) and rebuild lookup keys."""

        uniq = list({id(kd): kd for kd in alias_map.values()}.values())
        if self._discovery_cache_path is not None:
            for backend, key, disp in device_discovery_store.load_display_names(self._discovery_cache_path):
                if backend != "kasa":
                    continue
                for kd in uniq:
                    if key in {kd.mac_address, kd.host, kd.identifier}:
                        kd.set_display_name(disp)
                        break
        record_duplicate_preferred_labels(
            backend="kasa",
            devices=[(kd.mac_address, kd.preferred_label) for kd in uniq],
        )
        self._device_name_to_device = self._expand_kasa_lookup(uniq)
        macs = {kd.mac_address for kd in uniq}
        # A fresh roster starts the failed-poll count over for healthy plugs and forgets plugs that left.
        self._failed_polls = {
            mac: count
            for mac, count in self._failed_polls.items()
            if mac in macs and any(kd.mac_address == mac and kd.unresponsive for kd in uniq)
        }
        for mac in self._presence.missing_device_ids(DeviceFamilyId.KASA.value) - macs:
            self._presence.forget(DeviceFamilyId.KASA.value, mac)
        for kd in uniq:
            if kd.unresponsive:
                self._announce_unresponsive(kd)
            else:
                self._announce_responsive(kd)

    def rebuild_lookup_after_display_change(self) -> None:
        """Call after changing :meth:`~rule_engine.SwitchDevice.set_display_name` on a managed device."""

        if self._device_name_to_device is None:
            raise NotInitializedError
        uniq = list({id(kd): kd for kd in self._device_name_to_device.values()}.values())
        self._device_name_to_device = self._expand_kasa_lookup(uniq)

    async def fetch(self) -> None:
        if self._device_name_to_device is not None:
            raise AlreadyInitializedError

        await self._fetch_impl(force_discovery=self._force_discovery)

    async def forget_device(self, mac: str) -> bool:
        """Remove the plug with ``mac`` from the manager and the discovery cache.

        The operator chose to clean up after a device that stopped responding
        (domesti-bot#867). Its display name and UI preferences are kept, so a plug that
        is added back later gets its name and settings again. Returns ``False`` when no
        such plug is known.
        """

        current = self._device_name_to_device
        wanted = try_normalize_mac(mac)
        if current is None or wanted is None:
            return False
        uniq = list({id(kd): kd for kd in current.values()}.values())
        target = next((kd for kd in uniq if kd.mac_address == wanted), None)
        if target is None:
            return False
        host = target.host
        self._hosts_requiring_klap_auth.discard(host)
        self._skipped_klap_auth_configs.pop(host, None)
        self._failed_polls.pop(wanted, None)
        devices_by_host = {kd.host: kd for kd in uniq if kd is not target}
        # Write the new roster before any await so a concurrent rediscover keeps its result.
        self._finalize_kasa_lookup(devices_by_host)
        self._persist_discovery_cache(self._device_name_to_device or {})
        self._presence.forget(DeviceFamilyId.KASA.value, wanted)
        await self._disconnect_device_map({host: target})
        _LOGGER.info("Kasa: forgot %s (%s)", format_device_display(wanted, target.preferred_label), host)
        return True

    def get_device_by_alias(self, identifier: str) -> KasaDevice | None:
        if self._device_name_to_device is None:
            raise NotInitializedError
        return self._device_name_to_device.get(identifier)

    @property
    def has_credentials(self) -> bool:
        """``True`` when account credentials are available for KLAP-auth hosts.

        Driven by the constructor (``credentials=`` /
        ``username``+``password``) or by a subsequent
        :meth:`set_credentials` call. Credentials are only attached to
        hosts in :attr:`hosts_requiring_klap_auth`, not to anonymous devices.
        """

        return self._discovery_credentials is not None

    @property
    def hosts_requiring_klap_auth(self) -> tuple[str, ...]:
        """Hosts known to need account credentials for the KLAP handshake.

        Learned during fetch (anonymous ``AuthenticationError`` then success
        with credentials, or persistent failure without credentials) and
        restored from the discovery cache. Sorted for stable UI/tests.
        """

        return tuple(sorted(self._hosts_requiring_klap_auth))

    async def is_off(self, identifier: str) -> bool:
        return not await self.is_on(identifier)

    async def is_on(self, identifier: str) -> bool:
        kd = self._device_for(identifier)
        backend = kd._kDevice
        if backend is None:
            return kd.is_on
        try:
            await backend.update()
        except Exception:
            self._record_failed_poll(kd)
            raise
        self._record_successful_poll(kd)
        kd.set_power(backend.is_on)
        return backend.is_on

    @property
    def last_discovery_source(self) -> str | None:
        """``"cache"`` or ``"discovery"`` after :meth:`fetch`; ``None`` before.

        The CLI bootstrap reads this to annotate each backend's "ready" line
        with where the device list came from (SQLite vs. fresh UDP sweep).
        """

        return self._last_discovery_source

    async def recover_unresponsive(self, *, sweep: bool = False) -> tuple[str, ...]:
        """Reconnect unresponsive tiles whose plug answers again; return the recovered MACs.

        A plug that lost power stays in the manager as a protocol-silent stub
        (domesti-bot#867). Each stub is retried at its last known address and at the
        address ARP reports for its MAC; a plug that answers replaces its stub, and the
        discovery cache is rewritten with its current address. A plug that still does not
        answer is left exactly as it was. With ``sweep=True`` one UDP sweep also looks
        for stubs whose address changed and that ARP does not know yet.

        The result is dropped (and the new sessions closed) when a concurrent
        :meth:`rediscover` replaced the device map meanwhile: that sweep's view wins.
        Failures are logged at DEBUG; the caller decides how often to retry.
        """

        current = self._device_name_to_device
        if current is None or self._roster_replacements > 0:
            return ()
        stubs = {kd.mac_address: kd for kd in self.switches if kd.unresponsive}
        if not stubs or self._discovery_cache_path is None:
            return ()
        cached_by_mac = {
            mac: (host, cfg_dict, requires_klap_auth)
            for host, _alias, cfg_dict, requires_klap_auth, cached_mac in device_discovery_store.load_cached_configs(
                self._discovery_cache_path
            )
            if (mac := try_normalize_mac(cached_mac or "")) is not None
        }
        qtimeout = self._query_timeout if self._query_timeout is not None else DeviceConfig.DEFAULT_TIMEOUT
        # Sessions are collected into ``recovered`` as each attempt finishes, so a cycle cancelled
        # by the watcher (shutdown, a credentials save) can close every one it already opened.
        recovered: dict[str, KasaDevice] = {}
        try:
            await asyncio.gather(
                *(
                    self._recover_stub(mac, stub, cached_by_mac.get(mac), qtimeout, recovered)
                    for mac, stub in stubs.items()
                )
            )
            if sweep and len(recovered) < len(stubs):
                await self._sweep_for_unresponsive(
                    {mac for mac in stubs if mac not in recovered},
                    found=recovered,
                    live_hosts={kd.host for kd in current.values() if kd.kasa_protocol_device() is not None},
                    qtimeout=qtimeout,
                )
        except BaseException:
            await self._disconnect_device_map({kd.host: kd for kd in recovered.values()})
            raise
        if not recovered:
            return ()
        if self._device_name_to_device is not current or self._roster_replacements > 0:
            await self._disconnect_device_map({kd.host: kd for kd in recovered.values()})
            return ()
        devices_by_host: dict[str, KasaDevice] = {}
        candidates: list[KasaDevice] = []
        for kd in {id(kd): kd for kd in current.values()}.values():
            devices_by_host[kd.host] = kd
            # Judged now, with no await since the identity check: a poll that revived the plug
            # while recovery was connecting keeps its own session (the recovered one is closed).
            if kd.unresponsive and kd.mac_address in recovered:
                candidates.append(kd)
        unused = dict(recovered)
        replaced: list[KasaDevice] = []
        for stub in candidates:
            new = recovered[stub.mac_address]
            if new.host != stub.host and new.host in devices_by_host:
                # The recovered address is held by another tile (the cache is keyed by host, so
                # both cannot stay). Never drop a tile: leave this plug unresponsive and retry later.
                _LOGGER.warning(
                    "Kasa: %s answers at %s, which another tile holds; leaving it unresponsive",
                    format_device_display(stub.mac_address, stub.preferred_label),
                    new.host,
                )
                continue
            del devices_by_host[stub.host]
            devices_by_host[new.host] = new
            del unused[stub.mac_address]
            replaced.append(stub)
        restored = tuple(sorted(set(recovered) - set(unused)))
        if restored:
            for mac in restored:
                # The KLAP flag is tracked by host: follow a plug that came back at a new address.
                old_host = stubs[mac].host
                new_host = recovered[mac].host
                if new_host != old_host and old_host in self._hosts_requiring_klap_auth:
                    self._hosts_requiring_klap_auth.discard(old_host)
                    self._hosts_requiring_klap_auth.add(new_host)
                    self._skipped_klap_auth_configs.pop(old_host, None)
                self._failed_polls.pop(mac, None)
            # No await between the identity check above and this write: a rediscover or a
            # forget that lands while sessions close below keeps its result.
            self._finalize_kasa_lookup(devices_by_host)
            self._persist_discovery_cache(self._device_name_to_device or {})
        await self._disconnect_device_map({kd.host: kd for kd in unused.values()})
        await self._disconnect_device_map({kd.host: kd for kd in replaced})
        for mac in restored:
            _LOGGER.info(
                "Kasa: %s is answering again at %s; restored from unresponsive",
                format_device_display(mac, recovered[mac].preferred_label),
                recovered[mac].host,
            )
        return restored

    async def rediscover(self) -> None:
        """Drop connections and repopulate switches via UDP discovery (ignore SQLite cache reads).

        Cached configs are overwritten after a successful discovery, same as initial
        ``force_discovery=True`` startup behavior.

        If the UDP sweep raises after ``disconnect()``, attempt a cache-first
        reconnect so Settings hot-reload / REPL ``kasa-creds`` are not left with
        an empty device map until process restart.
        """

        with self._replacing_roster():
            await self.disconnect(clear_map=False)
            try:
                await self._fetch_impl(force_discovery=True)
            except Exception as udp_exc:
                try:
                    await self._fetch_impl(force_discovery=False)
                except Exception:
                    self._device_name_to_device = None
                    raise udp_exc from None
                raise udp_exc

    async def reload_from_cache(self) -> bool:
        """Replace the in-memory switch map from SQLite only (never UDP).

        Intended for the HTTP process when the CLI has rewritten
        ``device_discovery.sqlite``. On success the prior sessions are closed
        and the lookup map is replaced; the discovery table is **not**
        rewritten (the CLI owns those writes).

        Returns ``False`` (keeping the prior map) when there is no cache path,
        the manager is uninitialized, or reconnect fails in a way that would
        have fallen through to ``Discover.discover``. An empty cache clears the
        in-memory map and returns ``True``.
        """

        with self._replacing_roster():
            return await self._reload_from_cache_unguarded()

    def set_credentials(
        self,
        *,
        username: str,
        password: str,
    ) -> None:
        """Install Kasa/Tapo account credentials in memory for the next fetch.

        Used by the REPL ``kasa-creds`` command and Settings UI to
        recover from :class:`AuthenticationError` skips without
        restarting the process. Validates the same shape as the
        constructor: both ``username`` and ``password`` must be
        non-blank after strip, else :class:`ValueError`. Callers that
        need credentials to survive a restart should also write
        encrypted ``app_secrets`` (see :mod:`app.kasa_credentials`).
        """

        un = (username or "").strip()
        pw = (password or "").strip()
        if not un or not pw:
            raise ValueError(
                "Expected non-empty Kasa account email and password, got "
                f"username={un!r} password={'<set>' if pw else '<empty>'}"
            )
        self._discovery_credentials = Credentials(username=un, password=pw)

    @property
    def skipped_auth_hosts(self) -> tuple[str, ...]:
        """Hosts skipped during the last fetch due to KLAP ``AuthenticationError``.

        Sorted by host (stable for assertions and UI display). Empty
        tuple before the first fetch, after a successful re-fetch that
        cleared every previously skipped host, or after a fetch that
        only saw legacy XOR / unauth'd KLAP devices.
        """

        return tuple(sorted(self._last_skipped_auth_hosts))

    async def turn_off(self, identifier: str) -> None:
        await self._device_for(identifier).turn_off()

    async def turn_on(self, identifier: str) -> None:
        await self._device_for(identifier).turn_on()

    @property
    def switches(self) -> tuple[KasaDevice, ...]:
        """Discovered devices sorted by :meth:`~rule_engine.Device.preferred_label`."""

        if self._device_name_to_device is None:
            raise NotInitializedError
        devices = list({id(kd): kd for kd in self._device_name_to_device.values()}.values())
        devices.sort(key=lambda d: d.preferred_label.lower())
        return tuple(devices)

    async def sync_preferred_labels_to_vendor_aliases(self) -> int:
        """One-shot: push unique preferred labels onto Kasa vendor aliases.

        Skips when the preferred label is missing, equals the MAC, already
        matches the vendor alias, or would collide with another device's
        preferred label or existing alias (those stay as
        :mod:`app.device_label_conflicts` warnings). Persists the discovery
        cache so the next cache-first start sees the matched alias.

        Returns the number of devices whose alias was updated.
        """

        if self._device_name_to_device is None:
            raise NotInitializedError
        devices = list({id(kd): kd for kd in self._device_name_to_device.values()}.values())
        preferred_counts: dict[str, int] = {}
        alias_owner_mac: dict[str, str] = {}
        for kd in devices:
            preferred = kd.preferred_label.strip()
            if preferred and preferred.lower() != kd.mac_address.lower():
                key = preferred.lower()
                preferred_counts[key] = preferred_counts.get(key, 0) + 1
            alias = (getattr(kd._kDevice, "alias", None) or "").strip()
            if alias:
                alias_owner_mac.setdefault(alias.lower(), kd.mac_address)

        updated = 0
        alias_overrides: dict[str, str] = {}
        failed_macs: set[str] = set()
        progressed = True
        while progressed:
            progressed = False
            for kd in devices:
                mac_key = kd.mac_address.lower()
                if mac_key in alias_overrides or mac_key in failed_macs:
                    continue
                preferred = kd.preferred_label.strip()
                if not preferred or preferred.lower() == kd.mac_address.lower():
                    continue
                preferred_key = preferred.lower()
                if preferred_counts.get(preferred_key, 0) > 1:
                    continue
                owner = alias_owner_mac.get(preferred_key)
                if owner is not None and owner != kd.mac_address:
                    continue
                backend = kd._kDevice
                if backend is None:
                    continue
                current = (getattr(backend, "alias", None) or "").strip()
                if current.lower() == preferred_key:
                    continue
                try:
                    await backend.set_alias(preferred)
                except Exception:
                    _LOGGER.warning(
                        "Kasa: failed to set vendor alias on %s to %r",
                        format_device_display(kd.identifier, kd.preferred_label),
                        preferred,
                        exc_info=True,
                    )
                    failed_macs.add(mac_key)
                    continue
                if current:
                    old_key = current.lower()
                    if alias_owner_mac.get(old_key) == kd.mac_address:
                        del alias_owner_mac[old_key]
                alias_owner_mac[preferred_key] = kd.mac_address
                alias_overrides[mac_key] = preferred
                updated += 1
                progressed = True

        if updated:
            self._persist_discovery_cache(
                self._device_name_to_device,
                alias_overrides=alias_overrides,
                record_renames=False,
            )
            self.rebuild_lookup_after_display_change()
        return updated
