"""Tests for ``GET /v1/meta`` (package version + git commit for the web UI)."""

from __future__ import annotations

import argparse
from http import HTTPStatus

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.build_info import get_build_info


def _client() -> tuple[TestClient, FastAPI]:
    args = argparse.Namespace()
    app = create_app(args)
    return TestClient(app), app


def test_get_v1_meta_returns_version_and_commit_json() -> None:
    get_build_info.cache_clear()
    client, _app = _client()
    response = client.get("/v1/meta")
    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert isinstance(body["version"], str) and body["version"]
    assert isinstance(body["commit"], str) and body["commit"]


def test_get_v1_meta_uses_app_module_build_info_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.app as app_module

    monkeypatch.setattr(
        app_module,
        "get_build_info",
        lambda: ("9.9.9", "deadbeefcafe"),
    )
    client, _app = _client()
    response = client.get("/v1/meta")
    assert response.status_code == HTTPStatus.OK
    assert response.json() == {"version": "9.9.9", "commit": "deadbeefcafe"}


def test_v1_meta_path_registered_in_openapi() -> None:
    get_build_info.cache_clear()
    client, _app = _client()
    paths = client.get("/openapi.json").json().get("paths", {})
    assert "/v1/meta" in paths
