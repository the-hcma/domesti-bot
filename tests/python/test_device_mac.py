"""Tests for :mod:`app.device_mac`."""

from __future__ import annotations

import pytest

from app.device_mac import (
    is_normalized_mac,
    lookup_ip_via_arp_for_mac,
    lookup_mac_via_arp,
    mac_from_sonos_rincon,
    normalize_mac,
    try_normalize_mac,
)


def test_is_normalized_mac_accepts_lowercase_colon_form() -> None:
    assert is_normalized_mac("00:bd:3e:d5:f0:11") is True
    assert is_normalized_mac("00:BD:3E:D5:F0:11") is False
    assert is_normalized_mac("192.168.1.1") is False


def test_mac_from_sonos_rincon_extracts_embedded_mac() -> None:
    assert mac_from_sonos_rincon("RINCON_5CAAFD0A123401400") == "5c:aa:fd:0a:12:34"
    assert mac_from_sonos_rincon("not-a-rincon") is None


def test_normalize_mac_colon_and_bare_hex() -> None:
    assert normalize_mac("0:bd:3e:d5:f0:11") == "00:bd:3e:d5:f0:11"
    assert normalize_mac("00BD3ED5F011") == "00:bd:3e:d5:f0:11"
    assert normalize_mac("aabb.ccdd.eeff") == "aa:bb:cc:dd:ee:ff"


def test_normalize_mac_rejects_bare_hex_with_extra_chars() -> None:
    with pytest.raises(ValueError, match="12-hex-digit"):
        normalize_mac("00BD3ED5F011XX")


def test_normalize_mac_rejects_oversized_octet() -> None:
    with pytest.raises(ValueError, match="0..255"):
        normalize_mac("100:00:00:00:00:00")


def test_try_normalize_mac_returns_none_on_invalid() -> None:
    assert try_normalize_mac("not-a-mac") is None
    assert try_normalize_mac("00:bd:3e:d5:f0:11") == "00:bd:3e:d5:f0:11"


def test_lookup_ip_via_arp_for_mac_parses_macos_table(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = (
            "? (192.168.86.201) at 0:bd:3e:d5:f0:11 on en0 ifscope [ethernet]\n"
            "? (192.168.86.50) at aa:bb:cc:dd:ee:ff on en0 ifscope [ethernet]"
        )

    monkeypatch.setattr(
        "app.device_mac.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )
    assert lookup_ip_via_arp_for_mac("00:bd:3e:d5:f0:11") == "192.168.86.201"


def test_lookup_mac_via_arp_parses_macos_output(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Result:
        returncode = 0
        stdout = "? (192.168.86.201) at 0:bd:3e:d5:f0:11 on en0 ifscope [ethernet]"

    monkeypatch.setattr(
        "app.device_mac.subprocess.run",
        lambda *args, **kwargs: _Result(),
    )
    assert lookup_mac_via_arp("192.168.86.201") == "00:bd:3e:d5:f0:11"
