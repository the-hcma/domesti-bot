"""Resolve the GoTailwind Local Control Key from CLI, environment, or encrypted storage."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from app.db.secrets import SecretsDecryptError, load_tailwind_token_from_db

_LOGGER = logging.getLogger(__name__)

TailwindTokenSource = Literal["cli", "env", "database", "none"]


def resolve_tailwind_token(
    *,
    cli_token: str | None,
    cache_path: Path | None,
) -> tuple[str, TailwindTokenSource]:
    """Return ``(token, source)`` using precedence: CLI → env → encrypted DB.

    A stored token that no longer decrypts (the secrets key changed) is treated as absent and logged, so the
    garage-door manager and Settings do not crash; Settings reports it as stored but not configured.
    """
    cli = (cli_token or "").strip()
    if cli:
        return cli, "cli"
    env = (os.environ.get("TAILWIND_TOKEN") or "").strip()
    if env:
        return env, "env"
    if cache_path is not None:
        try:
            stored = load_tailwind_token_from_db(cache_path)
        except SecretsDecryptError:
            _LOGGER.warning(
                "Stored Tailwind token cannot be decrypted with the current secrets key; ignoring it "
                "(enter the token again in Settings > GoTailwind)"
            )
            return "", "none"
        if stored:
            return stored, "database"
    return "", "none"
