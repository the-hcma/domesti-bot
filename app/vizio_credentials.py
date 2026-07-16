"""Resolve per-TV SmartCast auth tokens from CLI, environment, or encrypted SQLite."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.db.secrets import (
    SecretsDecryptError,
    delete_app_secret,
    load_vizio_auth_token_from_db,
    save_vizio_auth_token_to_db,
    vizio_auth_token_stored_in_db,
)
from app.vizio_mac import normalize_mac

VizioAuthSource = Literal["cli", "env", "database", "none"]


def vizio_auth_secret_key_for_host(host: str) -> str:
    """Legacy SQLite ``app_secrets`` key keyed by TV IP/host."""
    return f"vizio_auth:{host.strip()}"


def vizio_auth_secret_key_for_mac(mac: str) -> str:
    """SQLite ``app_secrets`` key keyed by normalized TV MAC."""
    return f"vizio_auth:{normalize_mac(mac)}"


def resolve_vizio_auth_token(
    *,
    mac: str | None,
    host: str,
    cli_token: str | None,
    env_token: str | None,
    cache_path: Path | None,
) -> tuple[str, VizioAuthSource]:
    """Return ``(token, source)`` for one TV using CLI → per-MAC DB → legacy host DB → env."""
    cli = (cli_token or "").strip()
    if cli:
        return cli, "cli"
    if cache_path is not None:
        try:
            stored = load_vizio_auth_token_from_db(cache_path, mac=mac, host=host)
        except SecretsDecryptError:
            stored = None
        if stored:
            return stored, "database"
    env = (env_token or "").strip()
    if env:
        return env, "env"
    return "", "none"


def migrate_vizio_auth_token_host_to_mac(
    cache_path: Path,
    *,
    host: str,
    mac: str,
) -> None:
    """Prefer an existing MAC-scoped token; else re-key legacy host storage."""
    try:
        mac_token = load_vizio_auth_token_from_db(cache_path, mac=mac, host=None)
    except SecretsDecryptError:
        mac_token = None
    if mac_token:
        if vizio_auth_token_stored_in_db(cache_path, mac=None, host=host):
            delete_app_secret(cache_path, key=vizio_auth_secret_key_for_host(host))
        return
    try:
        host_token = load_vizio_auth_token_from_db(cache_path, mac=None, host=host)
    except SecretsDecryptError:
        host_token = None
    if host_token:
        save_vizio_auth_token_to_db(
            cache_path,
            mac=mac,
            host=host,
            token=host_token,
        )


def vizio_device_id_from_parts(*, mac: str | None, host: str, port: int) -> str:
    """Return the stable device id, preferring MAC when known."""
    from app.vizio_mac import device_id_for_vizio
    from app.vizio_smartcast_client import device_id_for

    if mac:
        return device_id_for_vizio(mac)
    return device_id_for(host, port)


def parse_vizio_setup_host(raw: str) -> str:
    """Return host portion of a setup IP / ``HOST:PORT`` string."""
    from app.vizio_smartcast_client import parse_host_spec

    host, _port = parse_host_spec(raw)
    return host
