"""Read-only credential probes for Settings Test buttons.

Ephemeral clients only — never call ``rediscover`` / ``disconnect`` on live
device managers.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gotailwind import Tailwind
from kasa import Device as KDevice
from kasa.credentials import Credentials
from kasa.deviceconfig import DeviceConfig
from kasa.exceptions import AuthenticationError, _ConnectionError

from app import device_discovery_store
from app.device_enums import SettingsCredentialsTestSource
from app.gotailwind_device_manager import (
    TailwindDiscoveryError,
    discover_tailwind_host,
)
from app.kasa_credentials import resolve_kasa_credentials
from app.kasa_device_manager import _connect_from_saved_config
from app.mytracks_service import (
    MyTracksSyncError,
    fetch_users_from_my_tracks,
    normalize_mytracks_base_url,
)
from app.mytracks_store import load_mytracks_config
from app.server_runtime import runtime
from app.tailwind_credentials import resolve_tailwind_token
from app.vizio_credentials import resolve_vizio_auth_token
from app.vizio_smartcast_client import (
    VizioSmartCastAuthError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
    VizioSmartCastError,
)


@dataclass(frozen=True, slots=True)
class CredentialsTestResult:
    """Outcome of a single Settings credential probe."""

    ok: bool
    detail: str
    source: SettingsCredentialsTestSource | None = None


class CredentialsTestUnavailableError(ValueError):
    """Raised when a probe cannot run (maps to HTTP 4xx)."""


async def probe_kasa_credentials(
    *,
    cache_path: Path | None,
    username: str | None = None,
    password: str | None = None,
) -> CredentialsTestResult:
    """Connect to known KLAP hosts with resolved credentials (ephemeral only)."""
    creds, source = _resolve_kasa_probe_credentials(
        cache_path=cache_path,
        username=username,
        password=password,
    )
    hosts = _klap_hosts_for_probe(cache_path=cache_path)
    if not hosts:
        raise CredentialsTestUnavailableError("No known KLAP hosts to probe; discover Kasa devices first")
    probeable: list[tuple[str, dict[str, Any]]] = []
    missing_profile: list[str] = []
    for host, cfg_dict in hosts:
        if cfg_dict is None:
            missing_profile.append(host)
            continue
        probeable.append((host, cfg_dict))
    if not probeable:
        raise CredentialsTestUnavailableError(
            f"No cached KLAP connection profile for {', '.join(missing_profile)}; run device discovery first"
        )
    successes: list[str] = []
    failures: list[str] = []
    auth_failures = 0
    for host in missing_profile:
        failures.append(f"{host}: no cached KLAP connection profile (run device discovery first)")
    for host, cfg_dict in probeable:
        try:
            state_label = await _probe_one_kasa_host(
                host,
                cfg_dict=cfg_dict,
                credentials=creds,
            )
        except AuthenticationError as exc:
            auth_failures += 1
            failures.append(f"{host}: {exc}")
            continue
        except Exception as exc:
            failures.append(f"{host}: {exc}")
            continue
        successes.append(f"{host}={state_label}")
    if successes and not failures:
        return CredentialsTestResult(
            ok=True,
            detail=f"KLAP auth ok on {len(successes)} host(s): {', '.join(successes)}",
            source=source,
        )
    if successes:
        return CredentialsTestResult(
            ok=False,
            detail=(
                f"KLAP auth partial ({len(successes)} ok, {len(failures)} failed): "
                f"{'; '.join(successes)}; {'; '.join(failures)}"
            ),
            source=source,
        )
    if auth_failures:
        return CredentialsTestResult(
            ok=False,
            detail=f"KLAP authentication failed: {'; '.join(failures)}",
            source=source,
        )
    return CredentialsTestResult(
        ok=False,
        detail=f"Could not reach KLAP hosts: {'; '.join(failures)}",
        source=source,
    )


async def probe_mytracks_credentials(
    *,
    cache_path: Path | None,
    password: str,
    domain: str | None = None,
    username: str | None = None,
) -> CredentialsTestResult:
    """Authenticated roster read against My Tracks (password is never stored)."""
    password_trimmed = password.strip()
    if not password_trimmed:
        raise CredentialsTestUnavailableError("Expected My Tracks admin password, got empty value")
    domain_trimmed = (domain or "").strip()
    username_trimmed = (username or "").strip()
    if not domain_trimmed or not username_trimmed:
        stored = load_mytracks_config(cache_path) if cache_path is not None else None
        if stored is not None:
            if not domain_trimmed:
                domain_trimmed = stored.domain.strip()
            if not username_trimmed:
                username_trimmed = stored.username.strip()
    if not domain_trimmed:
        raise CredentialsTestUnavailableError("Expected My Tracks domain, got empty value")
    if not username_trimmed:
        raise CredentialsTestUnavailableError("Expected My Tracks admin username, got empty value")
    # Password is always supplied in the request body (never stored).
    source = SettingsCredentialsTestSource.FORM
    try:
        base_url = normalize_mytracks_base_url(domain_trimmed)
        users = await asyncio.to_thread(
            fetch_users_from_my_tracks,
            base_url=base_url,
            password=password_trimmed,
            username=username_trimmed,
        )
    except MyTracksSyncError as exc:
        return CredentialsTestResult(ok=False, detail=str(exc), source=source)
    return CredentialsTestResult(
        ok=True,
        detail=f"My Tracks credentials ok ({len(users)} user(s))",
        source=source,
    )


async def probe_tailwind_token(
    *,
    cache_path: Path | None,
    cli_token: str | None,
    token: str | None = None,
    host: str | None = None,
) -> CredentialsTestResult:
    """One-shot Tailwind status read with an ephemeral client."""
    form_token = (token or "").strip()
    if form_token:
        resolved_token = form_token
        source = SettingsCredentialsTestSource.FORM
    else:
        resolved_token, resolved_source = resolve_tailwind_token(
            cli_token=cli_token,
            cache_path=cache_path,
        )
        if not resolved_token:
            raise CredentialsTestUnavailableError("No Tailwind token configured; enter a token or save one first")
        source = SettingsCredentialsTestSource(resolved_source)
    resolved_host = await _resolve_tailwind_probe_host(
        cache_path=cache_path,
        host=host,
    )
    client = Tailwind(host=resolved_host, token=resolved_token, request_timeout=8)
    try:
        await client.__aenter__()
        status = await client.status()
        door_count = len(status.doors)
    except Exception as exc:
        return CredentialsTestResult(
            ok=False,
            detail=f"Tailwind token probe failed at {resolved_host}: {exc}",
            source=source,
        )
    finally:
        await client.close()
    return CredentialsTestResult(
        ok=True,
        detail=f"Tailwind token ok at {resolved_host} ({door_count} door(s))",
        source=source,
    )


async def probe_vizio_auth(
    *,
    host: str,
    port: int,
    mac: str | None,
    cache_path: Path | None,
    cli_token: str | None,
    token: str | None = None,
) -> CredentialsTestResult:
    """One-shot SmartCast power read with an ephemeral client."""
    form_token = (token or "").strip()
    if form_token:
        resolved_token = form_token
        source = SettingsCredentialsTestSource.FORM
    else:
        resolved_token, resolved_source = resolve_vizio_auth_token(
            mac=mac,
            host=host,
            cli_token=cli_token,
            env_token=os.environ.get("VIZIO_AUTH_TOKEN"),
            cache_path=cache_path,
        )
        if not resolved_token:
            raise CredentialsTestUnavailableError("No Vizio auth token configured; enter a token or pair the TV first")
        source = SettingsCredentialsTestSource(resolved_source)
    client = VizioSmartCastClient(host, port=port, auth_token=resolved_token)
    try:
        power_on = await client.get_power_on()
    except VizioSmartCastAuthError as exc:
        return CredentialsTestResult(ok=False, detail=str(exc), source=source)
    except VizioSmartCastConnectionError as exc:
        return CredentialsTestResult(ok=False, detail=str(exc), source=source)
    except VizioSmartCastError as exc:
        return CredentialsTestResult(ok=False, detail=str(exc), source=source)
    finally:
        await client.aclose()
    power_label = "on" if power_on else "off"
    return CredentialsTestResult(
        ok=True,
        detail=f"Vizio auth ok at {host} (power {power_label})",
        source=source,
    )


_KASA_PROBE_HOST_CAP = 5
_KASA_PROBE_TIMEOUT_S = 8


def _klap_hosts_for_probe(
    *,
    cache_path: Path | None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """Return ``(host, config_dict | None)`` for known KLAP hosts, capped."""
    state = runtime.device_state
    host_to_config: dict[str, dict[str, Any] | None] = {}
    if state is not None:
        for host in state.kasa_mgr.hosts_requiring_klap_auth:
            host_to_config[host] = None
    if cache_path is not None:
        for host, _alias, cfg_dict, requires_klap, _mac in device_discovery_store.load_cached_configs(cache_path):
            if not requires_klap:
                continue
            host_to_config.setdefault(host, cfg_dict)
            if host_to_config[host] is None:
                host_to_config[host] = cfg_dict
    hosts = sorted(host_to_config.items(), key=lambda item: item[0])
    return hosts[:_KASA_PROBE_HOST_CAP]


async def _probe_one_kasa_host(
    host: str,
    *,
    cfg_dict: dict[str, Any],
    credentials: Credentials,
) -> str:
    """Connect, update, and disconnect one KLAP host; return ``on`` / ``off``."""
    dev: KDevice | None = None
    try:
        cfg = DeviceConfig.from_dict(cfg_dict)
        dev = await _connect_from_saved_config(
            cfg,
            credentials=credentials,
            timeout=_KASA_PROBE_TIMEOUT_S,
            raise_auth_failure=True,
        )
        if dev is None:
            raise _ConnectionError(f"Could not reach KLAP host {host} using cached profile")
        await dev.update()
        is_on = getattr(dev, "is_on", None)
        if is_on is True:
            return "on"
        if is_on is False:
            return "off"
        return "unknown"
    finally:
        if dev is not None:
            try:
                await dev.disconnect()
            except Exception:
                pass


def _resolve_kasa_probe_credentials(
    *,
    cache_path: Path | None,
    username: str | None,
    password: str | None,
) -> tuple[Credentials, SettingsCredentialsTestSource]:
    form_username = (username or "").strip()
    form_password = (password or "").strip()
    if form_username and form_password:
        return (
            Credentials(username=form_username, password=form_password),
            SettingsCredentialsTestSource.FORM,
        )
    if form_username or form_password:
        raise CredentialsTestUnavailableError(
            "Expected both username and password for form override, got a partial pair"
        )
    creds, resolved_source = resolve_kasa_credentials(cache_path=cache_path)
    if creds is None:
        raise CredentialsTestUnavailableError(
            "No Kasa credentials configured; enter account email and password or save them first"
        )
    return creds, SettingsCredentialsTestSource(resolved_source)


async def _resolve_tailwind_probe_host(
    *,
    cache_path: Path | None,
    host: str | None,
) -> str:
    form_host = (host or "").strip()
    if form_host:
        return form_host
    if cache_path is not None:
        cached = device_discovery_store.load_tailwind_host(cache_path)
        if cached:
            return cached
    env_host = (os.environ.get("TAILWIND_HOST") or "").strip()
    if env_host:
        return env_host
    try:
        return await discover_tailwind_host(timeout=8.0)
    except TailwindDiscoveryError as exc:
        raise CredentialsTestUnavailableError(str(exc)) from exc
