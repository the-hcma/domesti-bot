"""Transport policy for the My Tracks pairing URLs: HTTPS, with plain HTTP allowed only on a local network.

The relay keys travel in request headers (and, at pairing, in a request body), so a URL that sends them over plain
HTTP across the internet exposes them. This is a guard against an honest operator's misconfiguration, not a defense
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
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

TRANSPORT_BACKSLASH_ERROR = "Expected a URL without backslashes"
TRANSPORT_LAN_WARNING = (
    "The {label} uses plain HTTP on the local network ({host}): the relay keys cross it unencrypted; "
    "use https:// where you can"
)
TRANSPORT_NO_HOST_ERROR = "Expected a URL with a host"
# ``{tls_hint}`` is filled with TRANSPORT_TLS_PROXY_HINT.
TRANSPORT_REFUSAL = (
    "The {label} uses plain HTTP to a public host ({host}), which would send the relay keys "
    "unencrypted across the internet; {tls_hint}"
)
TRANSPORT_SCHEME_ERROR = "Expected an http or https URL"
TRANSPORT_STORED_PUBLIC_WARNING = "The {label} uses plain HTTP to a public host ({host}); re-pair with an https:// URL"
TRANSPORT_TLS_PROXY_HINT = "use an https:// URL (behind a TLS proxy, make sure it sends X-Forwarded-Proto: https)"
TRANSPORT_UNUSABLE = "The {label} cannot be used: {reason}"


@dataclass(frozen=True)
class TransportAssessment:
    """How a URL reaches its host: ``https``, or plain HTTP on ``loopback``, ``lan`` or to a ``public`` host."""

    url_class: TransportClass
    host: str


class TransportClass(StrEnum):
    """The transport classes a pairing URL can fall into."""

    HTTPS = "https"
    LAN = "lan"
    LOOPBACK = "loopback"
    PUBLIC = "public"


def assess_url(url: str) -> TransportAssessment:
    """Classify ``url`` by scheme and host. Raises ``ValueError`` when it has no usable host or is ambiguous."""
    trimmed = url.strip()
    if "\\" in trimmed:
        # WHATWG-style clients read a backslash as a path delimiter and urllib does not: they would disagree on host.
        raise ValueError(TRANSPORT_BACKSLASH_ERROR)
    parts = urlsplit(trimmed)
    host = _normalize_host(parts.hostname or "")
    if host == "":
        raise ValueError(TRANSPORT_NO_HOST_ERROR)
    if parts.scheme == "https":
        return TransportAssessment(TransportClass.HTTPS, host)
    if parts.scheme != "http":
        raise ValueError(TRANSPORT_SCHEME_ERROR)
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return _assess_name(host)
    if address.is_loopback:
        return TransportAssessment(TransportClass.LOOPBACK, host)
    if address.is_private or address.is_link_local or address in _SHARED_ADDRESS_SPACE:
        return TransportAssessment(TransportClass.LAN, host)
    return TransportAssessment(TransportClass.PUBLIC, host)


def transport_refusal(url: str, *, label: str) -> str | None:
    """A refusal message when ``url`` would send relay keys over plain HTTP to a public host, else ``None``."""
    try:
        assessment = assess_url(url)
    except ValueError as exc:
        return TRANSPORT_UNUSABLE.format(label=label, reason=exc)
    if assessment.url_class != TransportClass.PUBLIC:
        return None
    return TRANSPORT_REFUSAL.format(label=label, host=assessment.host, tls_hint=TRANSPORT_TLS_PROXY_HINT)


def transport_warning(url: str, *, label: str) -> str | None:
    """A warning when ``url`` uses plain HTTP on a local network (allowed, but the keys are not encrypted there).

    An ambiguous or unusable URL gives ``None``: :func:`transport_refusal` is the one that reports it.
    """
    try:
        assessment = assess_url(url)
    except ValueError:
        return None
    if assessment.url_class != TransportClass.LAN:
        return None
    return _lan_warning(assessment, label)


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
        if assessment.url_class == TransportClass.LAN:
            warnings.append(_lan_warning(assessment, label))
        elif assessment.url_class == TransportClass.PUBLIC:
            warnings.append(TRANSPORT_STORED_PUBLIC_WARNING.format(label=label, host=assessment.host))
    return warnings


_LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".internal", ".localdomain")
_IDEOGRAPHIC_FULL_STOPS = str.maketrans({"。": ".", "｡": "."})
_NUMERIC_LABEL = re.compile(r"^(0x[0-9a-f]+|[0-9]+)$")
# 100.64.0.0/10 is shared address space (carrier-grade NAT, Tailscale): not routable on the internet, so LAN-like.
_SHARED_ADDRESS_SPACE = ipaddress.ip_network("100.64.0.0/10")


def _assess_name(host: str) -> TransportAssessment:
    labels = host.split(".")
    if all(_NUMERIC_LABEL.fullmatch(label) for label in labels):
        # A number or hex spelling of an address ("134744072", "0x7f.1") that resolvers expand: never trust it.
        return TransportAssessment(TransportClass.PUBLIC, host)
    if host == "localhost" or host.endswith(".localhost"):
        return TransportAssessment(TransportClass.LOOPBACK, host)
    if len(labels) == 1 or host.endswith(_LOCAL_SUFFIXES):
        return TransportAssessment(TransportClass.LAN, host)
    return TransportAssessment(TransportClass.PUBLIC, host)


def _lan_warning(assessment: TransportAssessment, label: str) -> str:
    return TRANSPORT_LAN_WARNING.format(label=label, host=assessment.host)


def _normalize_host(hostname: str) -> str:
    """Lowercase ``hostname`` and fold Unicode dots and fullwidth forms the way the HTTP client's IDNA step does.

    IDNA (UTS 46) treats the ideographic full stops as label separators, so HTTP clients dial ``tracks.example.com``
    for ``tracks。example.com``.
    """
    return unicodedata.normalize("NFKC", hostname).translate(_IDEOGRAPHIC_FULL_STOPS).lower().rstrip(".")
