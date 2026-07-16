"""Optional LAN discovery helpers for Vizio SmartCast TVs."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError

import aiohttp

from app.vizio_smartcast_client import DEFAULT_VIZIO_PORT

_SSDP_GROUP = ("239.255.255.250", 1900)
_SSDP_TARGET = "urn:schemas-kinoma-com:device:shell:1"


@dataclass(frozen=True, slots=True)
class VizioDiscoveredHost:
    host: str
    port: int
    name: str
    model: str


async def discover_vizio_hosts_ssdp(*, timeout: float = 5.0) -> list[VizioDiscoveredHost]:
    """Best-effort SSDP sweep for Vizio DIAL devices; returns SmartCast port 7345."""
    responses = await _ssdp_msearch(_SSDP_TARGET, timeout=timeout)
    locations = sorted({loc for loc in responses if loc})
    found: list[VizioDiscoveredHost] = []
    seen_hosts: set[str] = set()
    async with aiohttp.ClientSession() as session:
        for location in locations:
            try:
                async with session.get(location, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                    xml = await resp.read()
            except (TimeoutError, aiohttp.ClientError):
                continue
            host_info = _ssdp_xml_to_host(location, xml)
            if host_info is None or host_info.host in seen_hosts:
                continue
            seen_hosts.add(host_info.host)
            found.append(host_info)
    found.sort(key=lambda item: item.host)
    return found


def _ssdp_xml_to_host(location: str, xml: bytes) -> VizioDiscoveredHost | None:
    try:
        root = ElementTree.fromstring(xml)
    except ParseError:
        return None
    device_el = _find_first(root, "device")
    if device_el is None:
        return None
    manufacturer = _find_text(device_el, "manufacturer")
    if manufacturer != "VIZIO":
        return None
    name = _find_text(device_el, "friendlyName") or "Vizio TV"
    model = _find_text(device_el, "modelName") or ""
    ip = urlsplit(location).hostname
    if not ip:
        return None
    return VizioDiscoveredHost(
        host=ip,
        port=DEFAULT_VIZIO_PORT,
        name=name,
        model=model,
    )


async def _ssdp_msearch(target: str, *, timeout: float) -> list[str]:
    loop = asyncio.get_running_loop()
    transport: asyncio.DatagramTransport | None = None
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            _SsdpProtocol,
            family=socket.AF_INET,
        )
        message = (
            "M-SEARCH * HTTP/1.1\r\n"
            f"HOST: {_SSDP_GROUP[0]}:{_SSDP_GROUP[1]}\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"ST: {target}\r\n"
            "MX: 3\r\n"
            "\r\n"
        )
        transport.sendto(message.encode("ascii"), _SSDP_GROUP)
        await asyncio.sleep(timeout)
        return list(protocol.locations)
    finally:
        if transport is not None:
            transport.close()


class _SsdpProtocol(asyncio.DatagramProtocol):
    def __init__(self) -> None:
        self.locations: list[str] = []

    def datagram_received(self, data: bytes, addr: tuple[str | int, int]) -> None:
        del addr
        for line in data.decode(errors="replace").splitlines():
            if line.lower().startswith("location:"):
                self.locations.append(line.split(":", 1)[1].strip())
                return


def _find_first(root: ElementTree.Element, tag: str) -> ElementTree.Element | None:
    for el in root.iter():
        local = el.tag.split("}", 1)[-1]
        if local == tag:
            return el
    return None


def _find_text(parent: ElementTree.Element, tag: str) -> str:
    el = _find_first(parent, tag)
    return (el.text or "").strip() if el is not None else ""
