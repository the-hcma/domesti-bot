"""Guard: uv.lock must match pyproject.toml version."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


def test_uv_lock_domesti_bot_version_matches_pyproject() -> None:
    repo_root = Path(__file__).resolve().parents[2]

    pyproject = repo_root / "pyproject.toml"
    uv_lock = repo_root / "uv.lock"

    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    pyproject_version = str(project.get("version") or "").strip()
    assert pyproject_version, "Expected pyproject.toml to declare [project].version, got empty"

    lock_text = uv_lock.read_text(encoding="utf-8")
    pattern = re.compile(
        r'^\[\[package\]\]\s*\nname = "domesti-bot"\s*\nversion = "([^"]+)"\s*$',
        re.MULTILINE,
    )
    match = pattern.search(lock_text)
    assert match is not None, 'Expected uv.lock to contain [[package]] name = "domesti-bot"'

    lock_version = match.group(1).strip()
    assert lock_version == pyproject_version, (
        "Expected uv.lock domesti-bot version to match pyproject.toml "
        f"({pyproject_version}), got {lock_version}. "
        "Run `uv lock` and commit the updated uv.lock."
    )

