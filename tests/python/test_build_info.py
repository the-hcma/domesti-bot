"""Tests for :mod:`app.build_info` (version and commit resolution)."""

from __future__ import annotations

import pytest

import app._build_metadata as build_meta
import app.build_info as build_info


def test_get_build_info_prefers_embedded_version_and_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_meta, "EMBEDDED_VERSION", "2.0.0")
    monkeypatch.setattr(build_meta, "EMBEDDED_COMMIT", "abcdef000000")
    build_info.get_build_info.cache_clear()
    assert build_info.get_build_info() == ("2.0.0", "abcdef000000")


def test_get_build_info_env_commit_overrides_embedded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_meta, "EMBEDDED_COMMIT", "111111111111")
    monkeypatch.setenv("DOMESTI_GIT_COMMIT", "fedcba" * 8)
    build_info.get_build_info.cache_clear()
    assert build_info.get_build_info()[1] == "fedcbafedcba"


def test_get_build_info_uses_patched_installed_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(build_meta, "EMBEDDED_VERSION", "")
    monkeypatch.setattr(build_info, "_read_installed_package_version", lambda: "3.4.5")
    build_info.get_build_info.cache_clear()
    ver, _commit = build_info.get_build_info()
    assert ver == "3.4.5"


def test_normalize_commit_token_shortens_long_hex() -> None:
    long_hex = "a" * 40
    assert build_info._normalize_commit_token(long_hex) == "a" * 12
