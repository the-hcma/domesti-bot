"""``_SettingsCacheControlMiddleware``: every ``/v1/settings`` response is ``Cache-Control: no-store``."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_app`` reads ``DOMESTI_API_KEY`` from the environment; keep it unset by default."""
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


def _client(*, cache_path: Path) -> TestClient:
    args = argparse.Namespace(discovery_cache=str(cache_path), tailwind_token=None)
    return TestClient(create_app(args))


def test_settings_get_response_is_no_store(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/tailwind-token")
    assert r.status_code == HTTPStatus.OK
    assert r.headers["cache-control"] == "no-store"


def test_settings_vizio_subpath_is_no_store(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/vizio/tvs")
    assert r.status_code == HTTPStatus.OK
    assert r.headers["cache-control"] == "no-store"


def test_settings_error_response_is_no_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOMESTI_API_KEY", "secret-key")
    client = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/v1/settings/tailwind-token")
    assert r.status_code == HTTPStatus.UNAUTHORIZED
    assert r.headers["cache-control"] == "no-store"


def test_non_settings_route_is_not_no_store(tmp_path: Path) -> None:
    client = _client(cache_path=tmp_path / "ui.sqlite")
    r = client.get("/health")
    assert r.status_code == HTTPStatus.OK
    assert r.headers.get("cache-control") != "no-store"
