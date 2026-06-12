"""Wake-on-LAN helper for Vizio TVs when the SmartCast HTTPS server is asleep."""

from __future__ import annotations

import re
import socket


def normalize_mac(mac: str) -> str:
    """Return lowercase colon-separated MAC or raise ``ValueError``."""
    cleaned = re.sub(r"[^0-9a-fA-F]", "", mac.strip())
    if len(cleaned) != 12:
        raise ValueError(f"Expected a 12-hex-digit MAC, got {mac!r}")
    pairs = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(p.lower() for p in pairs)


def send_wake_on_lan(
    mac: str,
    *,
    broadcast: str = "255.255.255.255",
) -> None:
    """Send a WoL magic packet for ``mac`` to ``broadcast``:9/7."""
    normalized = normalize_mac(mac)
    mac_bytes = bytes.fromhex(normalized.replace(":", ""))
    packet = b"\xff" * 6 + mac_bytes * 16
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast, 9))
        sock.sendto(packet, (broadcast, 7))
    finally:
        sock.close()
