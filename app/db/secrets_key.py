"""Resolve the Fernet master key from environment or ``domesti-secrets.json``."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

SecretsKeySource = Literal["env", "file", "none"]

_DEFAULT_SECRETS_FILENAME = "domesti-secrets.json"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def load_secrets_key_material() -> tuple[str | None, SecretsKeySource]:
    """Return ``(key material, source)`` without validating Fernet encoding."""
    env = (os.environ.get("DOMESTI_SECRETS_KEY") or "").strip()
    if env:
        return env, "env"
    path = secrets_json_path()
    if not path.is_file():
        return None, "none"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Expected {path} to contain JSON, got invalid JSON: {exc}"
        ) from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Expected {path} to contain a JSON object, got {type(raw).__name__}")
    value = raw.get("domesti_secrets_key")
    if value is None:
        return None, "none"
    key = str(value).strip()
    return (key, "file") if key else (None, "none")


def secrets_json_path() -> Path:
    """Path to the gitignored secrets file (override with ``DOMESTI_SECRETS_FILE``)."""
    override = (os.environ.get("DOMESTI_SECRETS_FILE") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _REPO_ROOT / _DEFAULT_SECRETS_FILENAME
