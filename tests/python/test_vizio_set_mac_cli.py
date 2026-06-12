"""Tests for the ``vizio-set-mac`` REPL command."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import Fernet

from app import device_discovery_store
from app.db.secrets import load_vizio_auth_token_from_db, save_vizio_auth_token_to_db
from app.domesti_bot_cli import _Theme, _repl_cmd_vizio_set_mac


@pytest.mark.asyncio
async def test_repl_cmd_vizio_set_mac_updates_cache_and_rekeys_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "vizio.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac=None,
        diid=None,
    )
    save_vizio_auth_token_to_db(
        db,
        host="192.168.86.201",
        token="Zmowtcpoxo",
    )
    vizio_mgr = MagicMock()
    vizio_mgr.fetch = AsyncMock()
    theme = _Theme(enabled=False)

    await _repl_cmd_vizio_set_mac(
        "192.168.86.201 00:bd:3e:d5:f0:11",
        cache_path=db,
        theme=theme,
        vizio_mgr=vizio_mgr,
    )

    out = capsys.readouterr().out
    assert "00:bd:3e:d5:f0:11" in out
    vizio_mgr.fetch.assert_awaited_once()
    rows = device_discovery_store.load_vizio_tvs(db)
    assert rows[0][4] == "00:bd:3e:d5:f0:11"
    assert load_vizio_auth_token_from_db(
        db,
        mac="00:bd:3e:d5:f0:11",
        host="192.168.86.201",
    ) == "Zmowtcpoxo"
    assert (
        load_vizio_auth_token_from_db(db, mac=None, host="192.168.86.201") is None
    )


@pytest.mark.asyncio
async def test_repl_cmd_vizio_set_mac_keeps_existing_mac_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "vizio.sqlite"
    device_discovery_store.upsert_vizio_tv(
        db,
        host="192.168.86.201",
        port=7345,
        display_name="Kitchen TV",
        model="V505M-K09",
        mac=None,
        diid=None,
    )
    save_vizio_auth_token_to_db(
        db,
        mac="00:bd:3e:d5:f0:11",
        host="192.168.86.201",
        token="mac-scoped-token",
    )
    save_vizio_auth_token_to_db(
        db,
        host="192.168.86.201",
        token="legacy-host-token",
    )
    theme = _Theme(enabled=False)

    await _repl_cmd_vizio_set_mac(
        "192.168.86.201 00:bd:3e:d5:f0:11",
        cache_path=db,
        theme=theme,
        vizio_mgr=None,
    )

    assert load_vizio_auth_token_from_db(
        db,
        mac="00:bd:3e:d5:f0:11",
        host="192.168.86.201",
    ) == "mac-scoped-token"
    assert load_vizio_auth_token_from_db(db, mac=None, host="192.168.86.201") is None


@pytest.mark.asyncio
async def test_repl_cmd_vizio_set_mac_reports_usage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    theme = _Theme(enabled=False)
    await _repl_cmd_vizio_set_mac(
        "192.168.86.201",
        cache_path=tmp_path / "vizio.sqlite",
        theme=theme,
        vizio_mgr=None,
    )
    err = capsys.readouterr().err
    assert "Usage: vizio-set-mac" in err
