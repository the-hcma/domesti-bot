"""Resolve Vizio TV MAC addresses and map MAC ↔ IP on the LAN."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess

_LOGGER = logging.getLogger(__name__)

_ARP_MAC_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}\b",
    re.I,
)
_ARP_IP_MAC_LINE_RE = re.compile(
    r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})",
    re.I,
)
_ARP_LINUX_LINE_RE = re.compile(
    r"(\d+\.\d+\.\d+\.\d+)\s+.*?\s+((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})",
    re.I,
)


def device_id_for_vizio(mac: str) -> str:
    """Stable UI / cache identifier for one TV (normalized MAC)."""
    return normalize_mac(mac)


def is_vizio_mac_device_id(device_id: str) -> bool:
    """True when ``device_id`` looks like a normalized MAC address."""
    try:
        normalize_mac(device_id)
    except ValueError:
        return False
    return True


def lookup_ip_via_arp_for_mac(mac: str) -> str | None:
    """Return the current IPv4 for ``mac`` from the local ARP table, if known."""
    try:
        target = normalize_mac(mac)
    except ValueError:
        return None
    try:
        completed = subprocess.run(
            ["arp", "-a"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.debug("ARP table scan failed while locating %s: %s", mac, exc)
        return None
    if completed.returncode != 0:
        return None
    for line in completed.stdout.splitlines():
        match = _ARP_IP_MAC_LINE_RE.search(line) or _ARP_LINUX_LINE_RE.search(line)
        if match is None:
            continue
        ip, raw_mac = match.group(1), match.group(2)
        try:
            if normalize_mac(raw_mac) == target:
                return ip
        except ValueError:
            continue
    return None


def lookup_mac_via_arp(host: str) -> str | None:
    """Best-effort MAC lookup from the host ARP/neighbor table."""
    host = host.strip()
    if not host:
        return None
    try:
        completed = subprocess.run(
            ["arp", "-n", host],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _LOGGER.debug("ARP lookup for %s failed: %s", host, exc)
        return None
    if completed.returncode != 0:
        return None
    match = _ARP_MAC_RE.search(completed.stdout)
    if match is None:
        return None
    try:
        return normalize_mac(match.group(0))
    except ValueError:
        return None


def normalize_mac(mac: str) -> str:
    """Return lowercase colon-separated MAC or raise ``ValueError``."""
    text = mac.strip()
    if re.search(r"[:.\-]", text):
        parts = re.split(r"[:.\-]", text)
        if len(parts) != 6:
            raise ValueError(f"Expected six MAC octets, got {mac!r}")
        try:
            return ":".join(f"{int(part, 16):02x}" for part in parts)
        except ValueError as exc:
            raise ValueError(f"Expected a valid MAC address, got {mac!r}") from exc
    cleaned = re.sub(r"[^0-9a-fA-F]", "", text)
    if len(cleaned) != 12:
        raise ValueError(f"Expected a 12-hex-digit MAC, got {mac!r}")
    pairs = [cleaned[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(p.lower() for p in pairs)


async def resolve_vizio_tv_ip(*, mac: str, fallback_host: str | None = None) -> str | None:
    """Locate the TV's current IP from ``mac``, else use ``fallback_host``."""
    ip = await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac)
    if ip is not None:
        return ip
    host = (fallback_host or "").strip()
    return host or None
