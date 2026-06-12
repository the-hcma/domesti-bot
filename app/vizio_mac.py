"""Resolve a Vizio TV MAC for Wake-on-LAN (SmartCast API or local ARP)."""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess

from app.vizio_smartcast_client import (
    VizioSmartCastAuthError,
    VizioSmartCastClient,
    VizioSmartCastConnectionError,
)
from app.vizio_wol import normalize_mac

_LOGGER = logging.getLogger(__name__)

_ARP_MAC_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}\b",
    re.I,
)


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


async def resolve_vizio_tv_mac(
    client: VizioSmartCastClient,
    *,
    host: str,
) -> str | None:
    """Return a normalized MAC from SmartCast network info, else local ARP."""
    try:
        mac = await client.fetch_network_mac()
        if mac is not None:
            return mac
    except (VizioSmartCastAuthError, VizioSmartCastConnectionError) as exc:
        _LOGGER.debug("SmartCast network MAC lookup for %s failed: %s", host, exc)
    return await asyncio.to_thread(lookup_mac_via_arp, host)
