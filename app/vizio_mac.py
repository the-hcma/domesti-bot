"""Vizio-specific MAC helpers built on :mod:`app.device_mac`."""

from __future__ import annotations

import asyncio

from app.device_mac import (
    is_normalized_mac,
    lookup_ip_via_arp_for_mac,
    lookup_mac_via_arp,
    normalize_mac,
    try_normalize_mac,
)

__all__ = [
    "device_id_for_vizio",
    "is_normalized_mac",
    "is_vizio_mac_device_id",
    "lookup_ip_via_arp_for_mac",
    "lookup_mac_via_arp",
    "normalize_mac",
    "resolve_vizio_tv_ip",
    "try_normalize_mac",
]


def device_id_for_vizio(mac: str) -> str:
    """Stable UI / cache identifier for one TV (normalized MAC)."""
    return normalize_mac(mac)


def is_vizio_mac_device_id(device_id: str) -> bool:
    """True when ``device_id`` looks like a normalized MAC address."""
    return try_normalize_mac(device_id) is not None


async def resolve_vizio_tv_ip(*, mac: str, fallback_host: str | None = None) -> str | None:
    """Locate the TV's current IP from ``mac``, else use ``fallback_host``."""
    ip = await asyncio.to_thread(lookup_ip_via_arp_for_mac, mac)
    if ip is not None:
        return ip
    host = (fallback_host or "").strip()
    return host or None
