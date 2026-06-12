"""Unit tests for :mod:`app.vizio_smartcast_client` helpers."""

from __future__ import annotations

import pytest

from app.vizio_smartcast_client import (
    DEFAULT_VIZIO_PORT,
    device_id_for,
    parse_host_spec,
)


def test_parse_host_spec_defaults_port() -> None:
    assert parse_host_spec("192.168.1.10") == ("192.168.1.10", DEFAULT_VIZIO_PORT)


def test_parse_host_spec_parses_explicit_port() -> None:
    assert parse_host_spec("192.168.1.10:7345") == ("192.168.1.10", 7345)


def test_parse_host_spec_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_host_spec("   ")


def test_device_id_for_omits_default_port() -> None:
    assert device_id_for("192.168.1.10", DEFAULT_VIZIO_PORT) == "192.168.1.10"


def test_device_id_for_includes_non_default_port() -> None:
    assert device_id_for("192.168.1.10", 7346) == "192.168.1.10:7346"
