"""Wheel layout checks for PyPI packaging (requires a prior ``uv build``)."""

from __future__ import annotations

import zipfile
from collections import Counter
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DIST_DIR = _REPO_ROOT / "dist"


def _latest_wheel() -> Path | None:
    wheels = sorted(_DIST_DIR.glob("domesti_bot-*.whl"), key=lambda p: p.stat().st_mtime)
    if not wheels:
        return None
    return wheels[-1]


@pytest.mark.skipif(_latest_wheel() is None, reason="no dist/*.whl — run uv build after pnpm run build")
def test_wheel_has_no_duplicate_static_paths() -> None:
    wheel = _latest_wheel()
    assert wheel is not None
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    static_names = [n for n in names if n.startswith("app/api/static/")]
    duplicates = [name for name, count in Counter(static_names).items() if count > 1]
    assert duplicates == [], f"duplicate static paths in wheel: {duplicates}"


@pytest.mark.skipif(_latest_wheel() is None, reason="no dist/*.whl — run uv build after pnpm run build")
def test_wheel_includes_web_bundle_when_built() -> None:
    bundle = _REPO_ROOT / "app/api/static/dist/main.js"
    if not bundle.is_file():
        pytest.skip("app/api/static/dist/main.js missing — run pnpm run build in web/ first")
    wheel = _latest_wheel()
    assert wheel is not None
    with zipfile.ZipFile(wheel) as archive:
        assert "app/api/static/dist/main.js" in archive.namelist()
