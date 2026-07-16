"""Tests for the ``kasa-creds`` REPL flow and skip-auth tracking.

Covers three layers:

1. :meth:`KasaDeviceManager.set_credentials` validates input and
   installs ``Credentials`` for the next fetch (no persistence).
2. :attr:`KasaDeviceManager.skipped_auth_hosts` tracks devices that
   exhausted recovery because the initial failure was ``AuthenticationError``.
3. :func:`app.domesti_bot_cli._repl_cmd_kasa_creds` collects credentials
   via an injected ``prompt_fn``, calls ``set_credentials``, and
   triggers ``rediscover``. Tests inject a canned-answer prompt so the
   helper is exercisable without prompt_toolkit's terminal layer.
4. :func:`app.domesti_bot_cli._maybe_print_kasa_auth_notice` only fires
   when the manager is missing credentials *and* the last fetch
   skipped at least one host.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet

from kasa.credentials import Credentials
from kasa.exceptions import AuthenticationError, _ConnectionError

from app.db.secrets import load_kasa_credentials_from_db
from app.domesti_bot_cli import (
    _Theme,
    _maybe_print_kasa_auth_notice,
    _repl_cmd_kasa_creds,
)
from app.kasa_device_manager import KasaDeviceManager


def _kdev_auth_fail(host: str) -> MagicMock:
    """A python-kasa-shaped mock whose ``update()`` raises ``AuthenticationError``."""

    dev = MagicMock(name=f"KDevice({host})")
    dev.host = host
    dev.alias = f"plug-{host}"
    dev.is_on = False
    dev.update = AsyncMock(side_effect=AuthenticationError("klap"))
    dev.disconnect = AsyncMock()
    dev.config = MagicMock()
    return dev


def _kdev_ok(host: str, alias: str = "ok") -> MagicMock:
    dev = MagicMock(name=f"KDevice({host})")
    dev.host = host
    dev.alias = alias
    dev.is_on = False
    dev.update = AsyncMock()
    dev.disconnect = AsyncMock()
    dev.config = MagicMock()
    return dev


def _make_prompt(answers: dict[str, str]):
    """Build a canned-answer prompt_fn for ``_repl_cmd_kasa_creds``.

    ``answers`` maps a substring of the prompt message to the value to
    return — keeps the test readable without depending on exact wording.
    """

    async def prompt_fn(message: str, is_password: bool) -> str:
        for needle, value in answers.items():
            if needle.lower() in message.lower():
                return value
        raise AssertionError(f"unexpected prompt: {message!r}")

    return prompt_fn


def test_set_credentials_installs_account_creds() -> None:
    mgr = KasaDeviceManager()
    assert mgr.has_credentials is False

    mgr.set_credentials(username="a@b.com", password="hunter2")

    assert mgr.has_credentials is True
    assert isinstance(mgr._discovery_credentials, Credentials)
    assert mgr._discovery_credentials.username == "a@b.com"
    assert mgr._discovery_credentials.password == "hunter2"


def test_set_credentials_rejects_blank_username() -> None:
    mgr = KasaDeviceManager()
    with pytest.raises(ValueError, match="non-empty"):
        mgr.set_credentials(username="  ", password="hunter2")


def test_set_credentials_rejects_blank_password() -> None:
    mgr = KasaDeviceManager()
    with pytest.raises(ValueError, match="non-empty"):
        mgr.set_credentials(username="a@b.com", password="")


def test_set_credentials_strips_surrounding_whitespace() -> None:
    mgr = KasaDeviceManager()
    mgr.set_credentials(username="  a@b.com  ", password="\thunter2\n")
    assert mgr._discovery_credentials is not None
    assert mgr._discovery_credentials.username == "a@b.com"
    assert mgr._discovery_credentials.password == "hunter2"


@pytest.mark.asyncio
async def test_fetch_records_skipped_auth_hosts() -> None:
    """A device that fails recovery starting from ``AuthenticationError`` lands in
    ``mgr.skipped_auth_hosts``; one that fails for non-auth reasons does not.
    """

    bad_auth = _kdev_auth_fail("192.168.86.216")
    bad_net = _kdev_ok("192.168.86.217")
    # Force the second device's update() to raise a plain connection
    # error (the manager treats this differently from auth failures).
    bad_net.update = AsyncMock(side_effect=_ConnectionError("read timeout"))
    discovered = {bad_auth.host: bad_auth, bad_net.host: bad_net}

    mgr = KasaDeviceManager()
    with (
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value=discovered),
        ),
        patch(
            "app.kasa_device_manager._connect_smart_plain_http",
            AsyncMock(side_effect=AuthenticationError("klap")),
        ),
        patch(
            "app.kasa_device_manager._connect_legacy_xor",
            AsyncMock(side_effect=ConnectionRefusedError("Connect call failed (..., 9999)")),
        ),
    ):
        await mgr.fetch()

    assert len(mgr.switches) == 0
    # Only the AuthenticationError-rooted host is tracked.
    assert mgr.skipped_auth_hosts == ("192.168.86.216",)


@pytest.mark.asyncio
async def test_fetch_keeps_klap_flag_when_credential_retry_fails_non_auth() -> None:
    """Anonymous AuthenticationError still marks KLAP if the credential retry times out.

    Without OR-accumulating auth_failure, a non-auth failure on the credential
    attempt would clear the KLAP flag and drop the host from Settings/cache.
    """

    from kasa.deviceconfig import (
        DeviceConfig,
        DeviceConnectionParameters,
        DeviceEncryptionType,
        DeviceFamily,
    )

    host = "192.168.86.218"
    klap = _kdev_auth_fail(host)
    klap.config = DeviceConfig(
        host=host,
        connection_type=DeviceConnectionParameters(
            DeviceFamily.SmartTapoPlug,
            DeviceEncryptionType.Klap,
        ),
    )
    klap.config.to_dict_control_credentials = MagicMock(return_value={"host": host, "timeout": 5})

    mgr = KasaDeviceManager()
    mgr.set_credentials(username="a@b.com", password="hunter2")
    legacy_xor = AsyncMock(side_effect=AssertionError("KLAP recovery must not try legacy XOR"))
    with (
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={host: klap}),
        ),
        patch(
            "app.kasa_device_manager._connect_smart_plain_http",
            AsyncMock(side_effect=AuthenticationError("klap")),
        ),
        patch(
            "app.kasa_device_manager._connect_legacy_xor",
            legacy_xor,
        ),
        patch(
            "app.kasa_device_manager.KDevice.connect",
            AsyncMock(side_effect=RuntimeError("handshake timed out")),
        ),
    ):
        await mgr.fetch()

    legacy_xor.assert_not_awaited()

    assert mgr.skipped_auth_hosts == (host,)
    assert mgr.hosts_requiring_klap_auth == (host,)


@pytest.mark.asyncio
async def test_known_klap_ingest_tracks_non_auth_credential_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Known KLAP hosts must be skip-tracked when credential connect times out.

    Cache reconnect already warns and tracks; UDP ingest must do the same so
    Settings still lists the host and the failure is not silent.
    """

    from kasa.deviceconfig import (
        DeviceConfig,
        DeviceConnectionParameters,
        DeviceEncryptionType,
        DeviceFamily,
    )

    host = "192.168.86.219"
    klap = _kdev_ok(host, alias="Tapo")
    klap.config = DeviceConfig(
        host=host,
        connection_type=DeviceConnectionParameters(
            DeviceFamily.SmartTapoPlug,
            DeviceEncryptionType.Klap,
        ),
    )
    klap.config.to_dict_control_credentials = MagicMock(return_value={"host": host, "timeout": 5})

    mgr = KasaDeviceManager()
    mgr.set_credentials(username="a@b.com", password="hunter2")
    mgr._hosts_requiring_klap_auth.add(host)

    with (
        caplog.at_level("WARNING"),
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={host: klap}),
        ),
        patch(
            "app.kasa_device_manager.KDevice.connect",
            AsyncMock(side_effect=RuntimeError("handshake timed out")),
        ),
    ):
        await mgr.fetch()

    assert mgr.skipped_auth_hosts == (host,)
    assert mgr.hosts_requiring_klap_auth == (host,)
    assert any("skipped device" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_fetch_clears_skipped_auth_hosts_after_success() -> None:
    """A subsequent rediscover with credentials clears stale skip markers."""

    auth_fail = _kdev_auth_fail("192.168.86.216")
    mgr = KasaDeviceManager()

    with (
        patch(
            "app.kasa_device_manager.Discover.discover",
            AsyncMock(return_value={auth_fail.host: auth_fail}),
        ),
        patch(
            "app.kasa_device_manager._connect_smart_plain_http",
            AsyncMock(side_effect=AuthenticationError("klap")),
        ),
        patch(
            "app.kasa_device_manager._connect_legacy_xor",
            AsyncMock(side_effect=ConnectionRefusedError("…")),
        ),
    ):
        await mgr.fetch()
    assert mgr.skipped_auth_hosts == ("192.168.86.216",)

    # Now mock a successful rediscover (different mock device, no auth error).
    ok = _kdev_ok("192.168.86.220", alias="Plug A")
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value={ok.host: ok}),
    ):
        await mgr.rediscover()
    assert mgr.skipped_auth_hosts == ()


@pytest.mark.asyncio
async def test_repl_cmd_kasa_creds_sets_credentials_and_rediscovers() -> None:
    """The REPL helper plumbs prompt → set_credentials → rediscover."""

    mgr = KasaDeviceManager()
    # Pre-stash a stale "skipped" marker so we can verify it clears.
    mgr._last_skipped_auth_hosts.append("192.168.86.216")

    rediscover = AsyncMock()
    with patch.object(KasaDeviceManager, "rediscover", rediscover):
        out_buf, err_buf = io.StringIO(), io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(err_buf):
            await _repl_cmd_kasa_creds(
                mgr,
                prompt_fn=_make_prompt({"email": "alice@example.com", "password": "hunter2"}),
                theme=_Theme(enabled=False),
            )

    rediscover.assert_awaited_once()
    assert mgr.has_credentials is True
    assert mgr._discovery_credentials is not None
    assert mgr._discovery_credentials.username == "alice@example.com"
    assert mgr._discovery_credentials.password == "hunter2"
    assert "no discovery cache" in out_buf.getvalue()


@pytest.mark.asyncio
async def test_repl_cmd_kasa_creds_persists_to_encrypted_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DOMESTI_BOT_SECRETS_KEY", Fernet.generate_key().decode("ascii"))
    db = tmp_path / "discovery.sqlite"
    mgr = KasaDeviceManager()
    rediscover = AsyncMock()
    with patch.object(KasaDeviceManager, "rediscover", rediscover):
        out_buf = io.StringIO()
        with redirect_stdout(out_buf), redirect_stderr(io.StringIO()):
            await _repl_cmd_kasa_creds(
                mgr,
                prompt_fn=_make_prompt({"email": "alice@example.com", "password": "hunter2"}),
                theme=_Theme(enabled=False),
                cache_path=db,
            )

    assert load_kasa_credentials_from_db(db) == ("alice@example.com", "hunter2")
    assert "encrypted discovery database" in out_buf.getvalue()


@pytest.mark.asyncio
async def test_repl_cmd_kasa_creds_handles_cancelled_input() -> None:
    """Ctrl-C / EOF during the prompt aborts cleanly without touching the manager."""

    mgr = KasaDeviceManager()

    async def cancelling_prompt(message: str, is_password: bool) -> str:
        raise KeyboardInterrupt

    rediscover = AsyncMock()
    with patch.object(KasaDeviceManager, "rediscover", rediscover):
        err_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
            await _repl_cmd_kasa_creds(mgr, prompt_fn=cancelling_prompt, theme=_Theme(enabled=False))

    rediscover.assert_not_called()
    assert mgr.has_credentials is False
    assert "cancelled" in err_buf.getvalue()


@pytest.mark.asyncio
async def test_repl_cmd_kasa_creds_surfaces_rediscover_failure() -> None:
    """A rediscover that raises is reported on stderr, not swallowed silently."""

    mgr = KasaDeviceManager()

    rediscover = AsyncMock(side_effect=RuntimeError("network down"))
    with patch.object(KasaDeviceManager, "rediscover", rediscover):
        err_buf = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err_buf):
            await _repl_cmd_kasa_creds(
                mgr,
                prompt_fn=_make_prompt({"email": "alice@example.com", "password": "hunter2"}),
                theme=_Theme(enabled=False),
            )

    # Credentials installed before the rediscover ran.
    assert mgr.has_credentials is True
    assert "rediscover failed" in err_buf.getvalue()
    assert "network down" in err_buf.getvalue()


def test_maybe_print_kasa_auth_notice_fires_when_skipped_and_no_creds() -> None:
    mgr = KasaDeviceManager()
    mgr._last_skipped_auth_hosts.extend(["192.168.86.216", "192.168.86.225", "192.168.86.234"])

    buf = io.StringIO()
    with redirect_stdout(buf):
        _maybe_print_kasa_auth_notice(mgr, theme=_Theme(enabled=False))

    out = buf.getvalue()
    assert "3 Kasa device(s) need account credentials" in out
    assert "kasa-creds" in out
    assert "KASA_USERNAME" in out


def test_maybe_print_kasa_auth_notice_silent_when_creds_already_set() -> None:
    """When creds *are* set and a host still failed auth, the per-device
    WARNING is the right surface — don't double-prompt at banner time."""

    mgr = KasaDeviceManager()
    mgr.set_credentials(username="alice@example.com", password="hunter2")
    mgr._last_skipped_auth_hosts.append("192.168.86.216")

    buf = io.StringIO()
    with redirect_stdout(buf):
        _maybe_print_kasa_auth_notice(mgr, theme=_Theme(enabled=False))

    assert buf.getvalue() == ""


def test_maybe_print_kasa_auth_notice_silent_when_no_skips() -> None:
    mgr = KasaDeviceManager()

    buf = io.StringIO()
    with redirect_stdout(buf):
        _maybe_print_kasa_auth_notice(mgr, theme=_Theme(enabled=False))

    assert buf.getvalue() == ""
