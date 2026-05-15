"""Resolve the GoTailwind Local Control Key from CLI, environment, or encrypted storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from app.db.secrets import load_tailwind_token_from_db

TailwindTokenSource = Literal["cli", "env", "database", "none"]


def resolve_tailwind_token(
    *,
    cli_token: str | None,
    cache_path: Path | None,
) -> tuple[str, TailwindTokenSource]:
    """Return ``(token, source)`` using precedence: CLI → env → encrypted DB."""
    cli = (cli_token or "").strip()
    if cli:
        return cli, "cli"
    env = (os.environ.get("TAILWIND_TOKEN") or "").strip()
    if env:
        return env, "env"
    if cache_path is not None:
        stored = load_tailwind_token_from_db(cache_path)
        if stored:
            return stored, "database"
    return "", "none"
