"""Transport policy for the My Tracks pairing URLs: HTTPS, with plain HTTP allowed only on a local network.

The relay keys travel in request headers (and, at pairing, in a request body), so a URL that sends them over plain
HTTP across the internet exposes them. This is a guard against an honest operator's misconfiguration, not a defence
against a hostile one: the decision is made from the URL text alone, with no DNS lookups (which would be slow and a side
channel), so a public DNS name that merely ends in a "local" suffix is trusted as local. ``https`` is always fine;
``http`` is fine on loopback, allowed with a warning on a private, link-local or shared-address (CGNAT, Tailscale)
address or a local-looking name (``.local``, ``.lan``, ``.home.arpa``, ``.internal``, ``.localdomain`` or a single
label), and refused for anything else, including numeric spellings of an address (``http://134744072``) and URLs
that different parsers read differently (a backslash).
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

TransportClass = Literal["https", "loopback", "lan", "public"]

_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
# 100.64.0.0/10 is shared address space (carrier-grade NAT, Tailscale): not routable on the internet, so LAN-like.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")


@dataclass(frozen=True)
class TransportAssessment:
    """How a URL reaches its host: ``https``, or plain HTTP on ``loopback``, ``lan`` or to a ``public`` host."""

    url_class: TransportClass
    host: str


def assess_url(url: str) -> TransportAssessment:
    """Classify ``url`` by scheme and host. Raises ``ValueError`` when it has no usable host or is ambiguous."""
    trimmed = url.strip()
    if "\\" in trimmed:
        # WHATWG-style clients read a backslash as a path delimiter and urllib does not: they would disagree on host.
        raise ValueError("Expected a URL without backslashes")
    parts = urlsplit(trimmed)
    host = (parts.hostname or "").lower().rstrip(".")
    if host == "":
        raise ValueError("Expected a URL with a host")
    if parts.scheme == "https":
        return TransportAssessment("https", host)
    if parts.scheme != "http":
        raise ValueError("Expected an http or https URL")
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return _assess_name(host)
    if address.is_loopback:
        return TransportAssessment("loopback", host)
    if address.is_private or address.is_link_local or address in _SHARED_ADDRESS_SPACE:
        return TransportAssessment("lan", host)
    return TransportAssessment("public", host)


def _assess_name(host: str) -> TransportAssessment:
    labels = host.split(".")
    if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        # A number or hex spelling of an address ("134744072", "0x7f.1") that resolvers expand: never trust it.
        return TransportAssessment("public", host)
    if host == "localhost" or host.endswith(".localhost"):
        return TransportAssessment("loopback", host)
    if len(labels) == 1 or host.endswith(_LOCAL_SUFFIXES):
        return TransportAssessment("lan", host)
    return TransportAssessment("public", host)


def transport_refusal(url: str, *, label: str) -> str | None:
    """A refusal message when ``url`` would send relay keys over plain HTTP to a public host, else ``None``."""
    try:
        assessment = assess_url(url)
    except ValueError as exc:
        return f"The {label} cannot be used: {exc}"
    if assessment.url_class != "public":
        return None
    return (
        f"The {label} uses plain HTTP to a public host ({assessment.host}), which would send the relay keys "
        "unencrypted across the internet; use an https:// URL (behind a TLS proxy, make sure it sends "
        "X-Forwarded-Proto: https)"
    )


def transport_warning(url: str, *, label: str) -> str | None:
    """A warning when ``url`` uses plain HTTP on a local network (allowed, but the keys are not encrypted there)."""
    assessment = assess_url(url)
    if assessment.url_class != "lan":
        return None
    return (
        f"The {label} uses plain HTTP on the local network ({assessment.host}): the relay keys cross it unencrypted; "
        "use https:// where you can"
    )


def transport_warnings(urls: Mapping[str, str | None]) -> list[str]:
    """Warnings for every non-empty URL in ``{label: url}`` (plain HTTP on a LAN, or already-stored public HTTP)."""
    warnings: list[str] = []
    for label, url in urls.items():
        if not url or not url.strip():
            continue
        try:
            assessment = assess_url(url)
        except ValueError:
            continue
        if assessment.url_class == "lan":
            warning = transport_warning(url, label=label)
        elif assessment.url_class == "public":
            warning = f"The {label} uses plain HTTP to a public host ({assessment.host}); re-pair with an https:// URL"
        else:
            warning = None
        if warning is not None:
            warnings.append(warning)
    return warnings
