"""Shared MAC normalization and ARP helpers for all device families."""

from __future__ import annotations

import logging
import re
import subprocess

_LOGGER = logging.getLogger(__name__)

_ARP_LINUX_LINE_RE = re.compile(
    r"(\d+\.\d+\.\d+\.\d+)\s+.*?\s+((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})",
    re.I,
)
_ARP_IP_MAC_LINE_RE = re.compile(
    r"\((\d+\.\d+\.\d+\.\d+)\)\s+at\s+((?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2})",
    re.I,
)
_ARP_MAC_RE = re.compile(
    r"\b(?:[0-9a-fA-F]{1,2}:){5}[0-9a-fA-F]{1,2}\b",
    re.I,
)
# Sonos UIDs commonly embed the wired MAC as 12 hex digits after ``RINCON_``.
_SONOS_RINCON_MAC_RE = re.compile(r"^RINCON_([0-9A-Fa-f]{12})", re.I)


def is_normalized_mac(device_id: str) -> bool:
    """True when ``device_id`` is already a lowercase colon-separated MAC."""
    text = device_id.strip()
    if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", text):
        return False
    return try_normalize_mac(text) == text


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
    return try_normalize_mac(match.group(0))


def mac_from_sonos_rincon(uid: str) -> str | None:
    """Extract a normalized MAC from a Sonos ``RINCON_<12hex>…`` UID when present."""
    text = uid.strip()
    match = _SONOS_RINCON_MAC_RE.match(text)
    if match is None:
        return None
    return try_normalize_mac(match.group(1))


def normalize_mac(mac: str) -> str:
    """Return lowercase colon-separated MAC or raise ``ValueError``.

    Accepts colon / hyphen octet separators (including mixed forms such as
    ``00:bd-3e:d5:f0:11``), Cisco-style dotted quads (``aabb.ccdd.eeff``), and
    bare 12-digit hex. Bare hex must be exactly twelve hex digits — non-hex
    characters are not stripped silently. Each octet must fit in one byte.
    """
    text = mac.strip()
    cisco = re.fullmatch(r"([0-9a-fA-F]{4})\.([0-9a-fA-F]{4})\.([0-9a-fA-F]{4})", text)
    if cisco is not None:
        text = "".join(cisco.groups())
    elif re.search(r"[:\-]", text):
        parts = re.split(r"[:\-]", text)
        if len(parts) != 6:
            raise ValueError(f"Expected six MAC octets, got {mac!r}")
        try:
            values = [int(part, 16) for part in parts]
        except ValueError as exc:
            raise ValueError(f"Expected a valid MAC address, got {mac!r}") from exc
        if any(value < 0 or value > 0xFF for value in values):
            raise ValueError(f"Expected MAC octets in 0..255, got {mac!r}")
        return ":".join(f"{value:02x}" for value in values)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", text):
        raise ValueError(f"Expected a 12-hex-digit MAC, got {mac!r}")
    pairs = [text[i : i + 2] for i in range(0, 12, 2)]
    return ":".join(p.lower() for p in pairs)


def try_normalize_mac(mac: str) -> str | None:
    """Return :func:`normalize_mac` result, or ``None`` when the input is invalid."""
    try:
        return normalize_mac(mac)
    except ValueError:
        return None
