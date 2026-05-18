"""Resolved package version and source commit for HTTP metadata and OpenAPI."""

from __future__ import annotations

import functools
import os
import subprocess
import tomllib
from pathlib import Path

from app import _build_metadata


def _normalize_commit_token(token: str) -> str:
    stripped = token.strip()
    if not stripped:
        return "unknown"
    if len(stripped) > 12:
        return stripped[:12]
    return stripped


def _read_installed_package_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        candidate = version("domesti-bot").strip()
    except PackageNotFoundError:
        return None
    return candidate or None


def _read_pyproject_version(repo_root: Path) -> str:
    path = repo_root / "pyproject.toml"
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return "0.0.0"
    project = data.get("project")
    if not isinstance(project, dict):
        return "0.0.0"
    ver = project.get("version")
    return ver if isinstance(ver, str) and ver else "0.0.0"


def _resolve_commit(repo_root: Path) -> str:
    for key in ("GITHUB_SHA", "DOMESTI_GIT_COMMIT"):
        raw = os.environ.get(key)
        if raw and raw.strip():
            return _normalize_commit_token(raw)
    embedded = getattr(_build_metadata, "EMBEDDED_COMMIT", "")
    if isinstance(embedded, str) and embedded.strip():
        return _normalize_commit_token(embedded)
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if proc.returncode != 0:
        return "unknown"
    out = proc.stdout.strip()
    return out if out else "unknown"


def _resolve_version(repo_root: Path) -> str:
    embedded = getattr(_build_metadata, "EMBEDDED_VERSION", "")
    if isinstance(embedded, str) and embedded.strip():
        return embedded.strip()
    installed = _read_installed_package_version()
    if installed:
        return installed
    return _read_pyproject_version(repo_root)


def format_cli_version_line(*, prog: str) -> str:
    """One-line version string for ``--version`` on console entry points."""
    version, commit = get_build_info()
    return f"{prog} {version} ({commit})"


@functools.lru_cache(maxsize=1)
def get_build_info() -> tuple[str, str]:
    """Return ``(package_version, commit_short_or_unknown)`` computed once per process."""
    repo_root = Path(__file__).resolve().parent.parent
    return (_resolve_version(repo_root), _resolve_commit(repo_root))
