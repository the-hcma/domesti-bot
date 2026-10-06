"""``GET /v1/settings/secrets-key``: counts per key generation, never key material or secret values."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.app import create_app
from app.db import secrets as secrets_module
from app.db.models import AppSecret
from app.db.secrets import (
    SecretsStoreError,
    rotate_app_secrets,
    save_kasa_credentials_to_db,
    save_tailwind_token_to_db,
    save_vizio_auth_token_to_db,
    secrets_key_status,
)
from app.db.session import discovery_session

_TOKEN = "-".join(["SENTINEL", "status", "token"])


def _key() -> str:
    return Fernet.generate_key().decode("ascii")


def _client(db: Path) -> TestClient:
    return TestClient(create_app(argparse.Namespace(discovery_cache=str(db), tailwind_token=None)))


@pytest.fixture(autouse=True)
def _no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DOMESTI_API_KEY", raising=False)


def test_status_walks_through_a_rotation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    client = _client(db)

    body = client.get("/v1/settings/secrets-key").json()
    assert body == {
        "configured": True,
        "source": "env",
        "generation_count": 1,
        "rows_total": 1,
        "rows_current": 1,
        "rows_on_older_generation": 0,
        "rows_unreadable": 0,
    }

    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    body = client.get("/v1/settings/secrets-key").json()
    assert (body["generation_count"], body["rows_current"], body["rows_on_older_generation"]) == (2, 0, 1)

    rotate_app_secrets(db)
    body = client.get("/v1/settings/secrets-key").json()
    assert (body["rows_current"], body["rows_on_older_generation"], body["rows_unreadable"]) == (1, 0, 0)


def test_status_counts_unreadable_rows_and_never_leaks_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", new)

    response = _client(db).get("/v1/settings/secrets-key")
    body = response.json()
    assert body["rows_unreadable"] == 1 and body["rows_current"] == 0
    # Counts and booleans only: the body has no string field that could carry key or secret material.
    assert {k for k, v in body.items() if isinstance(v, str)} == {"source"}
    assert old not in response.text and new not in response.text and _TOKEN not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_status_without_a_key_reports_unconfigured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(tmp_path / "missing.json"))
    body = _client(db).get("/v1/settings/secrets-key").json()
    assert body["configured"] is False
    assert body["source"] == "none"
    assert body["generation_count"] == 0


def test_status_with_an_invalid_key_in_the_list_is_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{_key()},nope")
    body = _client(tmp_path / "ui.sqlite").get("/v1/settings/secrets-key").json()
    assert body["configured"] is False and body["source"] == "none"


def test_buckets_always_add_up_to_the_total(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    old, new, stranger = _key(), _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", stranger)
    save_kasa_credentials_to_db(db, username="me@example.test", password="kasa-pass")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    save_vizio_auth_token_to_db(db, token="viz", mac="AA:BB:CC:DD:EE:FF")

    body = _client(db).get("/v1/settings/secrets-key").json()
    assert body["rows_total"] == 4
    assert (body["rows_current"], body["rows_on_older_generation"], body["rows_unreadable"]) == (1, 1, 2)
    assert body["rows_current"] + body["rows_on_older_generation"] + body["rows_unreadable"] == body["rows_total"]


def test_status_does_not_touch_the_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    with discovery_session(db) as session:
        before = [(r.key, bytes(r.ciphertext), r.updated_at) for r in session.scalars(select(AppSecret))]
    _client(db).get("/v1/settings/secrets-key")
    with discovery_session(db) as session:
        after = [(r.key, bytes(r.ciphertext), r.updated_at) for r in session.scalars(select(AppSecret))]
    assert after == before


def test_status_never_creates_or_alters_the_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", _key())
    db = tmp_path / "bare.sqlite"
    sqlite3.connect(db).close()
    before = db.read_bytes()
    body = secrets_key_status(db)
    assert (body.rows_total, body.rows_unreadable) == (0, 0)
    assert db.read_bytes() == before
    assert secrets_key_status(tmp_path / "missing.sqlite").rows_total == 0
    assert not (tmp_path / "missing.sqlite").exists()


@pytest.mark.asyncio
async def test_route_is_served_through_the_asgi_app_with_httpx_async_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    app = create_app(argparse.Namespace(discovery_cache=str(db), tailwind_token=None))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/settings/secrets-key")
    assert response.status_code == 200
    body = response.json()
    assert (body["generation_count"], body["rows_on_older_generation"], body["source"]) == (2, 1, "env")


def test_a_malformed_config_file_reports_unconfigured_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DOMESTI_BOT_SECRETS_KEY", raising=False)
    config = tmp_path / "domesti-bot.config.json"
    config.write_text("{ not json")
    monkeypatch.setenv("DOMESTI_BOT_CONFIG_FILE", str(config))
    response = _client(tmp_path / "ui.sqlite").get("/v1/settings/secrets-key")
    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert response.json()["source"] == "none"


def test_one_read_of_the_config_feeds_both_the_count_and_the_classification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ui.sqlite"
    old, new = _key(), _key()
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", old)
    save_tailwind_token_to_db(db, _TOKEN)
    reads = {"n": 0}
    real = secrets_module.load_secrets_key_material

    def _counting() -> tuple[str | None, str]:
        reads["n"] += 1
        return real()

    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", f"{new},{old}")
    monkeypatch.setattr(secrets_module, "load_secrets_key_material", _counting)
    status = secrets_key_status(db)
    assert reads["n"] == 1
    assert (status.generation_count, status.rows_on_older_generation, status.source) == (2, 1, "env")


def test_an_unreadable_database_is_an_error_not_zero_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", _key())
    db = tmp_path / "ui.sqlite"
    db.write_bytes(b"this is not a sqlite database" * 200)

    with pytest.raises(SecretsStoreError, match="unreadable"):
        secrets_key_status(db)
    response = _client(db).get("/v1/settings/secrets-key")
    assert response.status_code == 503
    assert "unreadable" in response.json()["detail"]
    assert db.read_bytes() == b"this is not a sqlite database" * 200
