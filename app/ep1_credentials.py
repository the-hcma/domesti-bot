"""Resolve the ESPHome Noise PSK for Everything Presence One devices."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from app.db.secrets import load_ep1_noise_psk_from_db

Ep1NoisePskSource = Literal["cli", "env", "database", "none"]


def resolve_ep1_noise_psk(
    *,
    cli_psk: str | None,
    cache_path: Path | None,
) -> tuple[str, Ep1NoisePskSource]:
    """Return ``(psk, source)`` using precedence: CLI → env → encrypted DB."""
    cli = (cli_psk or "").strip()
    if cli:
        return cli, "cli"
    env = (os.environ.get("EP1_NOISE_PSK") or "").strip()
    if env:
        return env, "env"
    if cache_path is not None:
        stored = load_ep1_noise_psk_from_db(cache_path)
        if stored:
            return stored, "database"
    return "", "none"
