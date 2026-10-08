"""Constant-time API key comparison and its two callers (operator key and my-tracks relay key)."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.api import api_key_compare
from app.api.api_key_compare import api_keys_match
from app.api.app import create_app


def test_api_keys_match_accepts_equal_keys() -> None:
    assert api_keys_match("operator-key", "operator-key") is True


@pytest.mark.parametrize(
    ("provided", "expected"),
    [
        ("operator-keX", "operator-key"),
        ("operator", "operator-key"),
        ("", "operator-key"),
        ("operator-key ", "operator-key"),
    ],
)
def test_api_keys_match_rejects_different_keys(provided: str, expected: str) -> None:
    assert api_keys_match(provided, expected) is False


def test_api_keys_match_handles_non_ascii_without_raising() -> None:
    assert api_keys_match("café", "cafe") is False
    assert api_keys_match("café", "café") is True


def test_api_keys_match_uses_hmac_compare_digest_on_utf8_bytes() -> None:
    fake_hmac = MagicMock()
    fake_hmac.compare_digest.return_value = True
    with patch.object(api_key_compare, "hmac", fake_hmac):
        assert api_keys_match("café", "other") is True
    fake_hmac.compare_digest.assert_called_once_with("café".encode(), b"other")


def _client(tmp_path: Path) -> TestClient:
    args = argparse.Namespace(discovery_cache=str(tmp_path / "ui.sqlite"), tailwind_token=None)
    return TestClient(create_app(args))


def test_operator_key_check_uses_the_constant_time_helper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", "operator-key")
    client = _client(tmp_path)
    with patch("app.api.api_scopes.api_keys_match", wraps=api_keys_match) as match:
        response = client.get("/v1/settings/tailwind-token", headers={"X-Domesti-Api-Key": "operator-key"})
    assert response.status_code == HTTPStatus.OK
    match.assert_called_once_with("operator-key", "operator-key")


@pytest.mark.parametrize("presented", ["operator-keX", "", "   "])
def test_operator_key_check_rejects_wrong_same_length_and_blank_keys(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    presented: str,
) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", "operator-key")
    client = _client(tmp_path)
    response = client.get("/v1/settings/tailwind-token", headers={"X-Domesti-Api-Key": presented})
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_operator_key_check_rejects_non_ascii_key_without_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", "operator-key")
    client = _client(tmp_path)
    response = client.get(
        "/v1/settings/tailwind-token",
        headers=[(b"X-Domesti-Api-Key", "café".encode("latin-1"))],
    )
    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_operator_key_check_is_skipped_when_no_key_is_configured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)
    client = _client(tmp_path)
    assert client.get("/v1/settings/tailwind-token").status_code == HTTPStatus.OK
