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
these devices. Pass ``username`` / ``password`` or ``credentials=``, or resolve via
:mod:`app.kasa_credentials`. Setting only one of username/password is an error.
Credentials are applied **per host**: UDP discovery stays anonymous; only hosts
marked ``requires_klap_auth`` (learned on first ``AuthenticationError`` and stored
in the discovery cache) receive account credentials on reconnect/update. Legacy
XOR / anonymous-KLAP devices never get credentials attached. When account
credentials are **not** configured, KLAP-auth hosts are skipped quietly (DEBUG)
and omitted from the device list — same as before credentials support — while
anonymous devices continue to work. They remain listed on
:attr:`hosts_requiring_klap_auth` / :attr:`skipped_auth_hosts` for Settings.
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
    raise_auth_failure: bool = False,
) -> KDevice | None:
    """Connect using a stored ``DeviceConfig``, mirroring discovery recovery paths.

    When ``raise_auth_failure`` is True and the initial failure was
    :class:`AuthenticationError`, re-raise instead of logging and returning
    ``None`` (used by Settings credential probes).
    """
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
        if _config_uses_klap(cfg):
            if raise_auth_failure and (
                isinstance(exc, AuthenticationError)
                or isinstance(last_exc, AuthenticationError)
            ):
                auth_exc = (
                    last_exc
                    if isinstance(last_exc, AuthenticationError)
                    else exc
                )
                raise AuthenticationError(
                    f"KLAP authentication failed for {cfg.host}"
                ) from auth_exc
            _LOGGER.warning(
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
        # Trust the commanded state. python-kasa often leaves ``is_on`` stale
        # until the next ``update()``; Vizio/Sonos/Tailwind pin cache the same way.
        self.set_power(False)

    async def turn_on(self) -> None:
        await self._kDevice.turn_on()
        self.set_power(True)


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
        use_creds = (
            prefer_credentials
            if prefer_credentials is not None
            else known_needs_auth
        )
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

    def _persist_discovery_cache(self, alias_map: dict[str, KasaDevice]) -> None:
        if self._discovery_cache_path is None:
            return
        # Preserve KLAP-auth hosts that were skipped (e.g. no credentials) so a
        # successful anonymous-only cache write does not forget them.
        prior_by_host = {
            host: (alias, cfg_dict, requires_klap_auth)
            for host, alias, cfg_dict, requires_klap_auth in (
                device_discovery_store.load_cached_configs(self._discovery_cache_path)
            )
        }
        rows: list[tuple[str, str | None, dict[str, Any], bool]] = []
        seen_hosts: set[str] = set()
        seen_ids: set[int] = set()
        for kd in alias_map.values():
            did = id(kd)
            if did in seen_ids:
                continue
            seen_ids.add(did)
            dev = kd._kDevice
            host = (dev.host or "").strip()
            if not host:
                continue
            cfg_dict = dev.config.to_dict_control_credentials(exclude_credentials=True)
            rows.append(
                (
                    host,
                    dev.alias,
                    cfg_dict,
                    host in self._hosts_requiring_klap_auth,
                )
            )
            seen_hosts.add(host)
        for host in sorted(self._hosts_requiring_klap_auth):
            if host in seen_hosts:
                continue
            prior = prior_by_host.get(host)
            if prior is not None:
                alias, cfg_dict, _requires = prior
                rows.append((host, alias, cfg_dict, True))
                continue
            skipped = self._skipped_klap_auth_configs.get(host)
            if skipped is None:
                continue
            alias, cfg_dict = skipped
            rows.append((host, alias, cfg_dict, True))
        rows.sort(key=lambda r: r[0])
        device_discovery_store.save_configs(self._discovery_cache_path, rows)

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
            suppress_warning = (
                quiet_anonymous_auth
                and auth_failure
                and credentials is None
            )
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
            suppress_warning = (
                quiet_anonymous_auth
                and auth_failure
                and credentials is None
            )
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
        self._skipped_klap_auth_configs = {}

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
                for host, _alias, cfg_dict, requires_klap_auth in cached:
                    if requires_klap_auth:
                        self._hosts_requiring_klap_auth.add(host)
                    else:
                        self._hosts_requiring_klap_auth.discard(host)
                    # Known KLAP-auth host and no credentials: skip quietly and
                    # keep using the cache for anonymous devices.
                    if requires_klap_auth and self._discovery_credentials is None:
                        self._last_skipped_auth_hosts.append(host)
                        _LOGGER.debug(
                            "Kasa: ignoring KLAP-auth host %s "
                            "(no account credentials configured)",
                            host,
                        )
                        continue
                    cfg = DeviceConfig.from_dict(cfg_dict)
                    # Only attach account credentials for hosts that need KLAP auth.
                    creds = (
                        self._discovery_credentials if requires_klap_auth else None
                    )
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
                                "Kasa: skipped KLAP-auth host %s "
                                "(credentials configured but handshake failed)",
                                host,
                            )
                            continue
                        cache_ok = False
                        break
                    # Device is already connected with the right credentials (or
                    # anonymously). Run update + SMART plain-HTTP / XOR recovery
                    # via the same path discovery ingest uses — bare update()
                    # would invalidate the whole cache on a recoverable failure.
                    ingested, was_auth_failure = await self._try_ingest_with_credentials(
                        dev,
                        qtimeout,
                        credentials=None,
                        log_failure=True,
                        quiet_anonymous_auth=(
                            not requires_klap_auth
                            and self._discovery_credentials is None
                        ),
                    )
                    if ingested is None and (
                        not requires_klap_auth
                        and was_auth_failure
                        and self._discovery_credentials is not None
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
                            ingested, was_auth2 = await self._try_ingest_with_credentials(
                                dev2,
                                qtimeout,
                                credentials=None,
                                log_failure=True,
                                quiet_anonymous_auth=False,
                            )
                            was_auth_failure = was_auth_failure or was_auth2
                            if ingested is not None:
                                self._hosts_requiring_klap_auth.add(host)
                    if ingested is not None:
                        kd = KasaDevice(ingested.alias or ingested.host, ingested)
                        devices_by_host[ingested.host] = kd
                        continue
                    if requires_klap_auth or was_auth_failure:
                        # KLAP (known or newly discovered): skip this host without
                        # invalidating the rest of the cache.
                        if host not in self._last_skipped_auth_hosts:
                            self._last_skipped_auth_hosts.append(host)
                        self._hosts_requiring_klap_auth.add(host)
                        try:
                            cfg_dict = dev.config.to_dict_control_credentials(
                                exclude_credentials=True,
                            )
                        except Exception:
                            cfg_dict = {"host": host}
                        self._skipped_klap_auth_configs[host] = (
                            getattr(dev, "alias", None),
                            cfg_dict,
                        )
                        if requires_klap_auth:
                            _LOGGER.warning(
                                "Kasa: skipped KLAP-auth host %s "
                                "(credentials configured but handshake failed)",
                                host,
                            )
                        with contextlib.suppress(Exception):
                            await dev.disconnect()
                        continue
                    cache_ok = False
                    with contextlib.suppress(Exception):
                        await dev.disconnect()
                    break
                if cache_ok:
                    self._finalize_kasa_lookup(devices_by_host)
                    self._persist_discovery_cache(self._device_name_to_device or {})
                    self._last_discovery_source = "cache"
                    return
                for kd in devices_by_host.values():
                    with contextlib.suppress(Exception):
                        await kd._kDevice.disconnect()
                devices_by_host = {}

        # Always discover anonymously; attach credentials per-host during ingest.
        if (
            self._discovery_target is None
            and self._query_timeout is None
            and self._discovery_timeout == 5
        ):
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

        If the UDP sweep raises after ``disconnect()``, attempt a cache-first
        reconnect so Settings hot-reload / REPL ``kasa-creds`` are not left with
        an empty device map until process restart.
        """

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
