"""Tests for Vizio MAC resolution helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.vizio_mac import (
    device_id_for_vizio,
    is_vizio_mac_device_id,
    lookup_ip_via_arp_for_mac,
    lookup_mac_via_arp,
    resolve_vizio_tv_ip,
)
from app.vizio_smartcast_client import (
    VizioSmartCastNotFoundError,
    extract_mac_from_payload,
    resolve_vizio_tv_mac,
)


def test_device_id_for_vizio_normalizes_mac() -> None:
    assert device_id_for_vizio("0:bd:3e:d5:f0:11") == "00:bd:3e:d5:f0:11"


def test_is_vizio_mac_device_id_accepts_normalized_mac() -> None:
    assert is_vizio_mac_device_id("00:bd:3e:d5:f0:11") is True
    assert is_vizio_mac_device_id("192.168.86.201") is False


def test_lookup_ip_via_arp_for_mac_parses_macos_table(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = (
            "? (192.168.86.201) at 0:bd:3e:d5:f0:11 on en0 ifscope [ethernet]\n"
            "? (192.168.86.50) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"
        )

    monkeypatch.setattr(
        "app.vizio_mac.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )
    assert lookup_ip_via_arp_for_mac("00:bd:3e:d5:f0:11") == "192.168.86.201"


@pytest.mark.asyncio
async def test_resolve_vizio_tv_ip_prefers_arp_over_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.vizio_mac.lookup_ip_via_arp_for_mac",
        lambda mac: "192.168.86.55",
    )
    ip = await resolve_vizio_tv_ip(
        mac="00:bd:3e:d5:f0:11",
        fallback_host="192.168.86.201",
    )
    assert ip == "192.168.86.55"


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
async def test_resolve_vizio_tv_mac_falls_back_to_arp_on_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = MagicMock()
    client.fetch_network_mac = AsyncMock(side_effect=VizioSmartCastNotFoundError("networkinfo missing"))
    monkeypatch.setattr(
        "app.vizio_smartcast_client.lookup_mac_via_arp",
        lambda host: "00:bd:3e:d5:f0:11",
    )
    mac = await resolve_vizio_tv_mac(client, host="192.168.86.201")
    assert mac == "00:bd:3e:d5:f0:11"


@pytest.mark.asyncio
async def test_resolve_vizio_tv_mac_prefers_smartcast_network() -> None:
    client = MagicMock()
    client.fetch_network_mac = AsyncMock(return_value="00:bd:3e:d5:f0:11")
    mac = await resolve_vizio_tv_mac(client, host="192.168.86.201")
    assert mac == "00:bd:3e:d5:f0:11"
