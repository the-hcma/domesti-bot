"""Resolve the Fernet master key from environment or ``domesti-secrets.json``."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Literal

from cryptography.fernet import Fernet

SecretsKeySource = Literal["env", "file", "none"]

_DEFAULT_SECRETS_FILENAME = "domesti-secrets.json"
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _git_repository_root() -> Path:
    """Return the checkout that owns the shared ``.git`` directory (same for all worktrees)."""
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return _REPO_ROOT
    git_common = Path(completed.stdout.strip())
    if git_common.name == ".git":
        return git_common.parent
    return _REPO_ROOT


def generate_fernet_key() -> str:
    """Return a new url-safe base64 Fernet key suitable for ``domesti_secrets_key``."""
    return Fernet.generate_key().decode("ascii")


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
    return _git_repository_root() / _DEFAULT_SECRETS_FILENAME


def write_secrets_json(domesti_secrets_key: str, *, path: Path | None = None) -> Path:
    """Write ``domesti-secrets.json`` (mode ``0600``) after validating the Fernet key."""
    key = domesti_secrets_key.strip()
    if not key:
        raise ValueError("Expected a non-empty domesti_secrets_key, got whitespace only")
    try:
        Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "Expected domesti_secrets_key to be a url-safe base64-encoded 32-byte Fernet key"
        ) from exc
    target = (path or secrets_json_path()).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"domesti_secrets_key": key}
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.chmod(target, 0o600)
    return target
