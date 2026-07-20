"""Hermetic tests for EP1 Noise PSK resolution."""

from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from app.db.secrets import save_ep1_noise_psk_to_db
from app.ep1_credentials import resolve_ep1_noise_psk


def test_resolve_ep1_noise_psk_prefers_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("EP1_NOISE_PSK", "env-psk")
    psk, source = resolve_ep1_noise_psk(cli_psk="cli-psk", cache_path=tmp_path / "missing.sqlite")
    assert psk == "cli-psk"
    assert source == "cli"


def test_resolve_ep1_noise_psk_env_then_database(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("EP1_NOISE_PSK", raising=False)
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", key)
    cache = tmp_path / "cache.sqlite"
    save_ep1_noise_psk_to_db(cache, "db-psk")

    psk, source = resolve_ep1_noise_psk(cli_psk=None, cache_path=cache)
    assert psk == "db-psk"
    assert source == "database"

    monkeypatch.setenv("EP1_NOISE_PSK", "env-psk")
    psk, source = resolve_ep1_noise_psk(cli_psk=None, cache_path=cache)
    assert psk == "env-psk"
    assert source == "env"


def test_resolve_ep1_noise_psk_none(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("EP1_NOISE_PSK", raising=False)
    psk, source = resolve_ep1_noise_psk(cli_psk=None, cache_path=tmp_path / "missing.sqlite")
    assert psk == ""
    assert source == "none"


def test_split_host_port_rejects_malformed() -> None:
    from app.domesti_bot_cli import _split_host_port
    from app.ep1_device_manager import DEFAULT_EP1_API_PORT

    assert _split_host_port("192.0.2.10", DEFAULT_EP1_API_PORT) == ("192.0.2.10", DEFAULT_EP1_API_PORT)
    assert _split_host_port("192.0.2.10:6054", DEFAULT_EP1_API_PORT) == ("192.0.2.10", 6054)
    with pytest.raises(ValueError, match="empty port"):
        _split_host_port("host:", DEFAULT_EP1_API_PORT)
    with pytest.raises(ValueError, match="numeric port"):
        _split_host_port("192.0.2.10:notaport", DEFAULT_EP1_API_PORT)
