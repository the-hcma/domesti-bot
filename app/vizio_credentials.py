"""Resolve per-TV SmartCast auth tokens from CLI, environment, or encrypted SQLite."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from app.db.secrets import SecretsDecryptError, load_vizio_auth_token_from_db

VizioAuthSource = Literal["cli", "env", "database", "none"]


def vizio_auth_secret_key(host: str) -> str:
    """SQLite ``app_secrets`` key for one TV host."""
    return f"vizio_auth:{host.strip()}"


def resolve_vizio_auth_token(
    *,
    host: str,
    cli_token: str | None,
    env_token: str | None,
    cache_path: Path | None,
) -> tuple[str, VizioAuthSource]:
    """Return ``(token, source)`` for one TV using CLI → per-host DB → env."""
    cli = (cli_token or "").strip()
    if cli:
        return cli, "cli"
    if cache_path is not None:
        try:
            stored = load_vizio_auth_token_from_db(cache_path, host=host)
        except SecretsDecryptError:
            stored = None
        if stored:
            return stored, "database"
    env = (env_token or "").strip()
    if env:
        return env, "env"
    return "", "none"
