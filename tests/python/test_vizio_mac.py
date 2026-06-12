"""Tests for Vizio MAC resolution helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.vizio_mac import lookup_mac_via_arp, resolve_vizio_tv_mac
from app.vizio_smartcast_client import extract_mac_from_payload


def test_extract_mac_from_payload_finds_nested_mac_field() -> None:
    payload = {
        "ITEMS": [
            {
                "VALUE": {
                    "WIRED_MAC_ADDRESS": "00:bd:3e:d5:f0:11",
                }
            }
        ]
    }
    assert extract_mac_from_payload(payload) == "00:bd:3e:d5:f0:11"


def test_lookup_mac_via_arp_parses_macos_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = "? (192.168.86.201) at 0:bd:3e:d5:f0:11 on en0 ifscope [ethernet]"

    monkeypatch.setattr(
        "app.vizio_mac.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )
    assert lookup_mac_via_arp("192.168.86.201") == "00:bd:3e:d5:f0:11"


@pytest.mark.asyncio
async def test_resolve_vizio_tv_mac_prefers_smartcast_network() -> None:
    client = MagicMock()
    client.fetch_network_mac = AsyncMock(return_value="00:bd:3e:d5:f0:11")
    mac = await resolve_vizio_tv_mac(client, host="192.168.86.201")
    assert mac == "00:bd:3e:d5:f0:11"
