"""Tests for :func:`app.kasa_device_manager._klap_auth_recovery_hint`.

When the initial ``dev.update()`` / saved-config connect fails with
:class:`AuthenticationError` we know auth (not network) is the root
cause — newer KLAP-encrypted Tapo/Kasa devices linked to the cloud need
the account email/password even on the LAN. The recovery WARNING gets
a self-diagnosing suffix so the operator doesn't have to puzzle out the
``Errno 61`` on the legacy XOR fallback port.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kasa.credentials import Credentials
from kasa.exceptions import AuthenticationError, _ConnectionError

from app.kasa_device_manager import (
    KasaDeviceManager,
    _klap_auth_recovery_hint,
)


def test_klap_auth_hint_empty_for_non_auth_failure() -> None:
    """Plain network / timeout failures get no auth-hint suffix."""

    hint = _klap_auth_recovery_hint(
        initial_exc=_ConnectionError("read timeout"),
        credentials=None,
    )
    assert hint == ""


def test_klap_auth_hint_points_at_env_vars_when_credentials_unset() -> None:
    """``AuthenticationError`` + no creds → tell the user to set the env vars."""

    hint = _klap_auth_recovery_hint(
        initial_exc=AuthenticationError("KLAP handshake failed"),
        credentials=None,
    )
    assert "KASA_USERNAME" in hint
    assert "KASA_PASSWORD" in hint
    assert "--force-discovery" in hint


def test_klap_auth_hint_flags_credential_mismatch_when_creds_present() -> None:
    """``AuthenticationError`` + creds set → likely wrong account email/password."""

    hint = _klap_auth_recovery_hint(
        initial_exc=AuthenticationError("KLAP handshake failed"),
        credentials=Credentials(username="someone@example.com", password="x"),
    )
    assert "may be wrong" in hint
    assert "KASA_USERNAME" in hint  # still names the env vars for context


@pytest.mark.asyncio
async def test_fetch_warning_surfaces_klap_hint_for_auth_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """End-to-end: a device whose ``update()`` raises ``AuthenticationError``
    and whose recovery exhausts gets the actionable suffix in its WARNING.
    """

    def _bad_kdev(host: str) -> MagicMock:
        dev = MagicMock(name=f"KDevice({host})")
        dev.host = host
        dev.alias = f"plug-{host}"
        dev.is_on = False
        # python-kasa raises ``AuthenticationError`` here for KLAP
        # devices that need account creds we don't have.
        dev.update = AsyncMock(side_effect=AuthenticationError("klap"))
        dev.disconnect = AsyncMock()
        dev.config = MagicMock()
        return dev

    bad = _bad_kdev("192.168.86.216")
    discovered = {bad.host: bad}

    mgr = KasaDeviceManager()

    # Force the recovery cascade to also fail so we hit the WARNING site.
    with patch(
        "app.kasa_device_manager.Discover.discover",
        AsyncMock(return_value=discovered),
    ), patch(
        "app.kasa_device_manager._connect_smart_plain_http",
        AsyncMock(side_effect=AuthenticationError("klap (plain http)")),
    ), patch(
        "app.kasa_device_manager._connect_legacy_xor",
        AsyncMock(
            side_effect=ConnectionRefusedError(
                "Unable to connect to the device: 192.168.86.216:9999"
            )
        ),
    ):
        with caplog.at_level(logging.WARNING, logger="app.kasa_device_manager"):
            await mgr.fetch()

    # The whole device dropped (recovery exhausted) — that's expected.
    assert len(mgr.switches) == 0

    warning_messages = [
        r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
    ]
    matching = [m for m in warning_messages if "192.168.86.216" in m and "skipped" in m]
    assert matching, warning_messages
    msg = matching[0]
    assert "AuthenticationError" in msg
    assert "KASA_USERNAME" in msg
    assert "--force-discovery" in msg
