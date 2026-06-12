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
``connection_type.https`` is false. On ``AuthenticationError`` or
``_ConnectionError``, we retry that LAN profile for ``SmartDevice`` instances, then XOR sweep.

Newer KLAP devices that were linked to the Kasa/Tapo cloud **require** your **account**
email and password for the LAN KLAP handshake — there is no anonymous LAN mode for
these devices. Pass ``username`` / ``password`` or ``credentials=``, or use
:meth:`credentials_from_env` when **both** ``KASA_USERNAME`` and ``KASA_PASSWORD`` are
set. Setting only one of them is treated as an error to avoid accidentally sending
partial credentials to ``Discover``. Without credentials, these devices fail the
initial handshake with :class:`AuthenticationError` and the recovery cascade
exhausts (HTTP-only KLAP also auths; legacy XOR :9999 is closed on KLAP-only
hardware); see :func:`_klap_auth_recovery_hint` for the user-facing message
appended to the ``skipped device`` WARNING.

Optional SQLite persistence (see :mod:`app.device_discovery_store`): pass
``discovery_cache_path`` to skip UDP discovery when every cached host reconnects.
Configs are saved without plaintext credentials (merge ``credentials`` /
``KASA_USERNAME`` + ``KASA_PASSWORD`` on load). Use ``force_discovery=True`` to refresh
the cache from the network.

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

import contextlib
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from kasa import Discover, Device as KDevice
from kasa.credentials import Credentials
from kasa.deviceconfig import (
    DeviceConfig,
    DeviceConnectionParameters,
    DeviceEncryptionType,
    DeviceFamily,
)
from kasa.exceptions import AuthenticationError, UnsupportedDeviceError, _ConnectionError

from app import device_discovery_store
from app.device_manager import AlreadyInitializedError, NotInitializedError, SwitchDeviceManager
from app.rule_engine import SwitchDevice

_LOGGER = logging.getLogger(__name__)


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
    return (
        "; the configured KASA_USERNAME / KASA_PASSWORD may be wrong "
        "for this device's KLAP handshake"
    )


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


async def _connect_smart_plain_http(
    cfg: DeviceConfig,
    *,
    credentials: Credentials | None,
    timeout: int,
) -> KDevice:
    """Reconnect SMART transports over HTTP when discovery incorrectly preferred HTTPS."""
    return await KDevice.connect(
        config=_plain_http_device_config(cfg, credentials=credentials, timeout=timeout)
    )


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
        f"No legacy XOR connection profile matched for {host} "
        "(KLAP discovery may have won the UDP race)."
    )


async def _connect_from_saved_config(
    cfg: DeviceConfig,
    *,
    credentials: Credentials | None,
    timeout: int,
) -> KDevice | None:
    """Connect using a stored ``DeviceConfig``, mirroring discovery recovery paths."""
    cfg = replace(cfg, timeout=timeout)
    if credentials is not None:
        cfg = replace(cfg, credentials=credentials)
    try:
        return await KDevice.connect(config=cfg)
    except (AuthenticationError, _ConnectionError) as exc:
        last_exc: BaseException = exc
        ctype = cfg.connection_type
        if ctype.device_family.value.startswith(
            "SMART"
        ) and ctype.encryption_type is DeviceEncryptionType.Klap:
            try:
                return await _connect_smart_plain_http(
                    cfg,
                    credentials=credentials,
                    timeout=timeout,
                )
            except Exception as ex:
                last_exc = ex
        try:
            return await _connect_legacy_xor(
                cfg.host,
                timeout=timeout,
                credentials=credentials,
            )
        except Exception as ex:
            _LOGGER.warning(
                "Kasa: skipped device at %s (%s); recovery failed (%s)%s",
                cfg.host,
                type(last_exc).__name__,
                ex,
                _klap_auth_recovery_hint(
                    initial_exc=exc, credentials=credentials
                ),
            )
            return None


class KasaDevice(SwitchDevice):

    __slots__ = ("_kDevice",)

    def __init__(
        self,
        identifier: str,
        kDevice: KDevice,
        *,
        display_name: str | None = None,
    ) -> None:
        super().__init__(identifier, display_name=display_name)
        self._kDevice = kDevice
        self.set_power(kDevice.is_on)

    async def turn_off(self) -> None:
        await self._kDevice.turn_off()
        self.set_power(self._kDevice.is_on)

    async def turn_on(self) -> None:
        await self._kDevice.turn_on()
        self.set_power(self._kDevice.is_on)


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
    ) -> None:
        self._device_name_to_device: dict[str, KasaDevice] | None = None
        self._discovery_target = (discovery_target or "").strip() or None
        self._discovery_timeout = discovery_timeout
        self._query_timeout = query_timeout
        self._discovery_cache_path = (
            Path(discovery_cache_path).expanduser().resolve()
            if discovery_cache_path
            else None
        )
        self._force_discovery = force_discovery
        # Set by :meth:`_fetch_impl` to ``"cache"`` (every saved config
        # reconnected without falling back to UDP) or ``"discovery"`` (full
        # ``Discover.discover`` broadcast sweep). ``None`` before first fetch.
        self._last_discovery_source: str | None = None
        # Hosts skipped during the most recent ``_fetch_impl`` because the
        # initial failure was an :class:`AuthenticationError` and every
        # recovery path exhausted. Read via :attr:`skipped_auth_hosts`;
        # the REPL ``kasa-creds`` command surfaces this list to suggest
        # rediscovery with account credentials.
        self._last_skipped_auth_hosts: list[str] = []

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

    async def disconnect(self) -> None:
        """Close TP-Link sessions and clear cached devices (call ``fetch`` to reconnect).

        Clears the alias map even if closing a device fails, so the manager never stays
        half-initialized.
        """
        if self._device_name_to_device is None:
            return
        cached = self._device_name_to_device
        self._device_name_to_device = None
        seen: set[int] = set()
        for kd in cached.values():
            dev = kd._kDevice
            did = id(dev)
            if did in seen:
                continue
            seen.add(did)
            with contextlib.suppress(Exception):
                await dev.disconnect()

    async def _ingest_discovered_device(
        self, dev: KDevice, qtimeout: int
    ) -> KDevice | None:
        """Run ``update()`` with the same SMART plain-HTTP / XOR recovery as discovery."""
        try:
            await dev.update()
            return dev
        except (AuthenticationError, _ConnectionError) as exc:
            from kasa.smart.smartdevice import SmartDevice

            cfg_before = dev.config
            host = dev.host
            with contextlib.suppress(Exception):
                await dev.disconnect()

            last_exc: BaseException = exc
            recovered = False
            if isinstance(dev, SmartDevice):
                try:
                    dev = await _connect_smart_plain_http(
                        cfg_before,
                        credentials=self._discovery_credentials,
                        timeout=qtimeout,
                    )
                    recovered = True
                except Exception as ex:
                    last_exc = ex
            if not recovered:
                try:
                    dev = await _connect_legacy_xor(
                        host,
                        timeout=qtimeout,
                        credentials=self._discovery_credentials,
                    )
                except Exception as ex:
                    _LOGGER.warning(
                        "Kasa: skipped device at %s (%s); recovery failed (%s)%s",
                        host,
                        type(last_exc).__name__,
                        ex,
                        _klap_auth_recovery_hint(
                            initial_exc=exc,
                            credentials=self._discovery_credentials,
                        ),
                    )
                    if isinstance(exc, AuthenticationError):
                        self._last_skipped_auth_hosts.append(host)
                    return None
            return dev

    def _persist_discovery_cache(self, alias_map: dict[str, KasaDevice]) -> None:
        if self._discovery_cache_path is None:
            return
        rows: list[tuple[str, str | None, dict[str, Any]]] = []
        seen: set[int] = set()
        for kd in alias_map.values():
            did = id(kd)
            if did in seen:
                continue
            seen.add(did)
            dev = kd._kDevice
            cfg_dict = dev.config.to_dict_control_credentials(exclude_credentials=True)
            rows.append((dev.host, dev.alias, cfg_dict))
        rows.sort(key=lambda r: r[0])
        device_discovery_store.save_configs(self._discovery_cache_path, rows)

    def _expand_kasa_lookup(self, devices: list[KasaDevice]) -> dict[str, KasaDevice]:
        """Build the multi-key lookup: host (always) + alias / display name (when unique).

        Host is the only identifier guaranteed unique on the LAN, so
        every device is registered under its host first. Aliases and
        display names register as *additional* lookup keys; if two
        devices share an alias (e.g. two outlets the user named ``"Plug"``
        in the Kasa app) only the first claimant wins that key — the
        duplicate stays reachable via its host and a warning is logged.
        """

        lookup: dict[str, KasaDevice] = {}
        for kd in devices:
            host = (kd._kDevice.host or "").strip()
            if host:
                lookup[host] = kd
        for kd in devices:
            host = (kd._kDevice.host or "").strip()
            candidate_keys: list[str] = [kd.identifier]
            if kd.preferred_label != kd.identifier:
                candidate_keys.append(kd.preferred_label)
            for key in candidate_keys:
                if not key or key == host:
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
                        existing._kDevice.host,
                        host,
                        host,
                    )
        return lookup

    async def _fetch_impl(self, *, force_discovery: bool) -> None:
        qtimeout = (
            self._query_timeout
            if self._query_timeout is not None
            else DeviceConfig.DEFAULT_TIMEOUT
        )
        # Reset per-fetch state so subsequent rediscovers don't carry
        # stale auth-failure markers from a previous attempt that ran
        # without credentials.
        self._last_skipped_auth_hosts = []

        # Dedup by ``host`` (the LAN identifier, guaranteed unique by
        # virtue of being an IP address) rather than by ``alias`` —
        # users routinely give multiple physical outlets the same name
        # in the Kasa/Tapo app (e.g. two "Plug" or "Lamp") and the old
        # alias-keyed map silently dropped all-but-one of them.
        devices_by_host: dict[str, KasaDevice] = {}

        if self._discovery_cache_path is not None and not force_discovery:
            cached = device_discovery_store.load_cached_configs(self._discovery_cache_path)
            if cached:
                cache_ok = True
                for _host, cfg_dict in cached:
                    cfg = DeviceConfig.from_dict(cfg_dict)
                    dev = await _connect_from_saved_config(
                        cfg,
                        credentials=self._discovery_credentials,
                        timeout=qtimeout,
                    )
                    if dev is None:
                        cache_ok = False
                        break
                    finalized = await self._ingest_discovered_device(dev, qtimeout)
                    if finalized is None:
                        cache_ok = False
                        break
                    kd = KasaDevice(finalized.alias or finalized.host, finalized)
                    devices_by_host[finalized.host] = kd
                if cache_ok:
                    self._finalize_kasa_lookup(devices_by_host)
                    self._persist_discovery_cache(self._device_name_to_device or {})
                    self._last_discovery_source = "cache"
                    return
                for kd in devices_by_host.values():
                    with contextlib.suppress(Exception):
                        await kd._kDevice.disconnect()
                devices_by_host = {}

        if (
            self._discovery_target is None
            and self._discovery_credentials is None
            and self._query_timeout is None
            and self._discovery_timeout == 5
        ):
            devices = await Discover.discover()
        else:
            discover_kw: dict[str, Any] = {"discovery_timeout": self._discovery_timeout}
            if self._discovery_target is not None:
                discover_kw["target"] = self._discovery_target
            if self._discovery_credentials is not None:
                discover_kw["credentials"] = self._discovery_credentials
            if self._query_timeout is not None:
                discover_kw["timeout"] = self._query_timeout
            devices = await Discover.discover(**discover_kw)

        discovered_count = len(devices)
        try:
            for discovered in devices.values():
                finalized = await self._ingest_discovered_device(discovered, qtimeout)
                if finalized is None:
                    continue
                kd = KasaDevice(finalized.alias or finalized.host, finalized)
                devices_by_host[finalized.host] = kd
        except BaseException:
            for kd in devices_by_host.values():
                with contextlib.suppress(Exception):
                    await kd._kDevice.disconnect()
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

        self._finalize_kasa_lookup(devices_by_host)
        self._persist_discovery_cache(self._device_name_to_device or {})
        self._last_discovery_source = "discovery"

    def _finalize_kasa_lookup(self, alias_map: dict[str, KasaDevice]) -> None:
        """Apply SQLite display names (keyed by device host) and rebuild lookup keys."""

        uniq = list({id(kd): kd for kd in alias_map.values()}.values())
        if self._discovery_cache_path is not None:
            for backend, key, disp in device_discovery_store.load_display_names(
                self._discovery_cache_path
            ):
                if backend != "kasa":
                    continue
                for kd in uniq:
                    if kd._kDevice.host == key:
                        kd.set_display_name(disp)
                        break
        self._device_name_to_device = self._expand_kasa_lookup(uniq)

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

    def get_device_by_alias(self, identifier: str) -> KasaDevice | None:
        if self._device_name_to_device is None:
            raise NotInitializedError
        return self._device_name_to_device.get(identifier)

    @property
    def has_credentials(self) -> bool:
        """``True`` when account creds will be sent to ``Discover.discover``.

        Driven by the constructor (``credentials=`` /
        ``username``+``password``) or by a subsequent
        :meth:`set_credentials` call from the REPL.
        """

        return self._discovery_credentials is not None

    async def is_off(self, identifier: str) -> bool:
        return not await self.is_on(identifier)

    async def is_on(self, identifier: str) -> bool:
        kd = self._device_for(identifier)
        await kd._kDevice.update()
        kd.set_power(kd._kDevice.is_on)
        return kd._kDevice.is_on

    @property
    def last_discovery_source(self) -> str | None:
        """``"cache"`` or ``"discovery"`` after :meth:`fetch`; ``None`` before.

        The CLI bootstrap reads this to annotate each backend's "ready" line
        with where the device list came from (SQLite vs. fresh UDP sweep).
        """

        return self._last_discovery_source

    async def rediscover(self) -> None:
        """Drop connections and repopulate switches via UDP discovery (ignore SQLite cache reads).

        Cached configs are overwritten after a successful discovery, same as initial
        ``force_discovery=True`` startup behavior.
        """

        await self.disconnect()
        await self._fetch_impl(force_discovery=True)

    def set_credentials(
        self,
        *,
        username: str,
        password: str,
    ) -> None:
        """Install Kasa/Tapo account credentials in memory for the next fetch.

        Used by the REPL ``kasa-creds`` command to recover from
        :class:`AuthenticationError` skips without restarting the
        process. Validates the same shape as the constructor: both
        ``username`` and ``password`` must be non-blank after strip,
        else :class:`ValueError`. Credentials are **not** persisted
        anywhere — to survive a restart, the user must set
        ``KASA_USERNAME`` + ``KASA_PASSWORD`` in the environment (or
        the systemd ``EnvironmentFile=``).
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
