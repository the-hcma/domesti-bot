"""Resolve the ESPHome Noise PSK for Everything Presence One devices."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from app.db.secrets import SecretsDecryptError, load_ep1_noise_psk_from_db

_LOGGER = logging.getLogger(__name__)

Ep1NoisePskSource = Literal["cli", "env", "database", "none"]


def resolve_ep1_noise_psk(
    *,
    cli_psk: str | None,
    cache_path: Path | None,
) -> tuple[str, Ep1NoisePskSource]:
    """Return ``(psk, source)`` using precedence: CLI → env → encrypted DB.

    A stored key that no longer decrypts (the secrets key changed) is treated as absent and logged, so the EP1
    manager, calibration routes and settings do not crash; Settings reports it as stored but not configured.
    """
    cli = (cli_psk or "").strip()
    if cli:
        return cli, "cli"
    env = (os.environ.get("EP1_NOISE_PSK") or "").strip()
    if env:
        return env, "env"
    if cache_path is not None:
        try:
            stored = load_ep1_noise_psk_from_db(cache_path)
        except SecretsDecryptError:
            _LOGGER.warning(
                "Stored EP1 Noise PSK cannot be decrypted with the current secrets key; ignoring it "
                "(enter the key again in Settings > EP1)"
            )
            return "", "none"
        if stored:
            return stored, "database"
    return "", "none"
