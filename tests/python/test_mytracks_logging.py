"""Tests for my-tracks logging helpers."""

from __future__ import annotations

from app.mytracks_logging import mytracks_log_host


def test_mytracks_log_host_strips_scheme_and_path() -> None:
    assert mytracks_log_host("https://mytracks.hcma.info/login/") == "mytracks.hcma.info"


def test_mytracks_log_host_accepts_bare_hostname() -> None:
    assert mytracks_log_host("mytracks.hcma.info") == "mytracks.hcma.info"


def test_mytracks_log_host_empty_is_unset_label() -> None:
    assert mytracks_log_host("") == "(unset)"
