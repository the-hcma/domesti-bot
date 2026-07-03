"""Resolve Kasa/Tapo KLAP account credentials from environment or encrypted storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from kasa import Credentials

from app.db.secrets import SecretsDecryptError, load_kasa_credentials_from_db

KasaCredentialsSource = Literal["env", "database", "none"]


def resolve_kasa_credentials(
    *,
    cache_path: Path | None,
) -> tuple[Credentials | None, KasaCredentialsSource]:
    """Return ``(credentials, source)`` using precedence: env → encrypted DB.

    Both username and password must be present in the chosen source; partial
    pairs are ignored (same rule as the historical env-only path). Undecryptable
    database rows (e.g. after a Fernet key change) are treated as absent so
    bootstrap and settings routes do not crash.
    """
    env_creds = _credentials_from_env()
    if env_creds is not None:
        return env_creds, "env"
    if cache_path is not None:
        try:
            stored = load_kasa_credentials_from_db(cache_path)
        except SecretsDecryptError:
            return None, "none"
        if stored is not None:
            username, password = stored
            return Credentials(username=username, password=password), "database"
    return None, "none"


def _credentials_from_env() -> Credentials | None:
    un = (os.environ.get("KASA_USERNAME") or "").strip()
    pw = (os.environ.get("KASA_PASSWORD") or "").strip()
    if un and pw:
        return Credentials(username=un, password=pw)
    return None
