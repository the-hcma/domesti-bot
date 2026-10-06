"""CORS is off by default and opt-in through ``DOMESTI_CORS_ORIGINS`` (never a wildcard)."""

from __future__ import annotations

import argparse
import logging
from http import HTTPStatus
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.app import _cors_allowed_origins, create_app

_ALLOWED = "https://ui.example.test"
_OTHER = "https://evil.example.test"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)
    monkeypatch.delenv("DOMESTI_CORS_ORIGINS", raising=False)


def _client(tmp_path: Path) -> TestClient:
    args = argparse.Namespace(discovery_cache=str(tmp_path / "ui.sqlite"), tailwind_token=None)
    return TestClient(create_app(args))


def _preflight(client: TestClient, origin: str) -> Any:
    return client.options(
        "/v1/settings/tailwind-token",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-Domesti-Api-Key",
        },
    )


def test_no_cors_headers_by_default(tmp_path: Path) -> None:
    client = _client(tmp_path)
    response = client.get("/health", headers={"Origin": _OTHER})
    assert response.status_code == HTTPStatus.OK
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers


def test_default_preflight_is_not_granted(tmp_path: Path) -> None:
    response = _preflight(_client(tmp_path), _OTHER)
    assert "access-control-allow-origin" not in response.headers


def test_allowlisted_origin_is_granted_and_only_that_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", f"{_ALLOWED}/, ")
    client = _client(tmp_path)

    allowed = _preflight(client, _ALLOWED)
    assert allowed.status_code == HTTPStatus.OK
    assert allowed.headers["access-control-allow-origin"] == _ALLOWED
    assert "access-control-allow-credentials" not in allowed.headers

    denied = _preflight(client, _OTHER)
    assert denied.status_code == HTTPStatus.BAD_REQUEST
    assert "access-control-allow-origin" not in denied.headers

    simple = client.get("/health", headers={"Origin": _OTHER})
    assert "access-control-allow-origin" not in simple.headers


def test_wildcard_and_malformed_entries_are_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    bad = [
        "*",
        "null",
        "ui.example.test",
        "ftp://x.example.test",
        f"{_ALLOWED}/path",
        f"{_ALLOWED}?q=1",
        f"{_ALLOWED}#frag",
        "https://user@ui.example.test",
        "https://ui.example.test:abc",
        "https://ui.example.test:99999",
    ]
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", ",".join([*bad, _ALLOWED, _ALLOWED]))
    with caplog.at_level(logging.WARNING, logger="app.api"):
        origins = _cors_allowed_origins()
    assert origins == [_ALLOWED]
    assert caplog.text.count("[cors] ignoring DOMESTI_CORS_ORIGINS entry") == len(bad)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("HTTPS://UI.Example.test", "https://ui.example.test"),
        ("https://ui.example.test:443", "https://ui.example.test"),
        ("http://ui.example.test:80/", "http://ui.example.test"),
        ("http://ui.example.test:8080", "http://ui.example.test:8080"),
        ("https://ui.example.test:80", "https://ui.example.test:80"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://192.168.1.10:5173", "http://192.168.1.10:5173"),
    ],
)
def test_entries_are_normalized_to_the_form_browsers_send(
    monkeypatch: pytest.MonkeyPatch, entry: str, expected: str
) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", entry)
    assert _cors_allowed_origins() == [expected]


def test_a_normalized_entry_matches_the_origin_a_browser_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", "HTTPS://UI.Example.test:443")
    response = _client(tmp_path).get("/health", headers={"Origin": _ALLOWED})
    assert response.headers["access-control-allow-origin"] == _ALLOWED


def test_allowed_origin_gets_cors_headers_on_a_simple_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", _ALLOWED)
    response = _client(tmp_path).get("/health", headers={"Origin": _ALLOWED})
    assert response.status_code == HTTPStatus.OK
    assert response.headers["access-control-allow-origin"] == _ALLOWED
    assert "access-control-allow-credentials" not in response.headers


def test_preflight_advertises_only_the_allowed_methods_and_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", _ALLOWED)
    client = _client(tmp_path)
    granted = _preflight(client, _ALLOWED)
    assert set(granted.headers["access-control-allow-methods"].split(", ")) == {
        "DELETE",
        "GET",
        "PATCH",
        "POST",
        "PUT",
    }
    # Starlette always adds the CORS-safelisted request headers (Accept, Content-Type, ...) itself.
    allowed_headers = set(granted.headers["access-control-allow-headers"].split(", "))
    assert {"Content-Type", "X-Domesti-Api-Key"} <= allowed_headers
    assert "Authorization" not in allowed_headers
    refused = client.options(
        "/v1/settings/tailwind-token",
        headers={
            "Origin": _ALLOWED,
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "Authorization",
        },
    )
    assert refused.status_code == HTTPStatus.BAD_REQUEST


def test_wildcard_alone_grants_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_CORS_ORIGINS", "*")
    client = _client(tmp_path)
    response = client.get("/health", headers={"Origin": _OTHER})
    assert "access-control-allow-origin" not in response.headers
