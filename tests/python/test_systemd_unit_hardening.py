"""Baseline hardening of the shipped systemd user unit (domesti-bot#879)."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USER_UNIT = _REPO_ROOT / "etc" / "systemd" / "domesti-bot.service"
# What the process needs: sockets for HTTP, mDNS/SSDP and UDP discovery (AF_INET*), the C library's
# interface listing (AF_NETLINK) and local IPC (AF_UNIX). Raw packet sockets are not used.
_REQUIRED_ADDRESS_FAMILIES = frozenset({"AF_INET", "AF_INET6", "AF_NETLINK", "AF_UNIX"})


def _service_directives(path: Path) -> dict[str, str]:
    """Return the ``[Service]`` key/value pairs of a unit file (last assignment wins)."""

    directives: dict[str, str] = {}
    in_service = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        header = re.fullmatch(r"\[(\w+)\]", line)
        if header is not None:
            in_service = header.group(1) == "Service"
            continue
        if in_service and "=" in line:
            key, _, value = line.partition("=")
            directives[key.strip()] = value.strip()
    return directives


def test_user_unit_forbids_gaining_privileges() -> None:
    assert _service_directives(_USER_UNIT).get("NoNewPrivileges") == "true"


def test_user_unit_has_a_private_tmp_and_a_read_only_system() -> None:
    directives = _service_directives(_USER_UNIT)
    assert directives.get("PrivateTmp") == "true"
    assert directives.get("ProtectSystem") == "full"


def test_user_unit_restricts_address_families_to_what_the_service_uses() -> None:
    families = set(_service_directives(_USER_UNIT).get("RestrictAddressFamilies", "").split())
    assert families == _REQUIRED_ADDRESS_FAMILIES, (
        f"Expected {sorted(_REQUIRED_ADDRESS_FAMILIES)}, got {sorted(families)}"
    )


def test_user_unit_keeps_the_home_directory_writable() -> None:
    # The service writes its log, discovery cache and rules under the home directory and its checkout.
    directives = _service_directives(_USER_UNIT)
    assert "ProtectHome" not in directives
    assert directives.get("ProtectSystem") != "strict"


def test_the_service_directive_parser_ignores_other_sections_and_comments(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Unit]\nNoNewPrivileges=false\n[Service]\n# PrivateTmp=false\nPrivateTmp=true\n", encoding="utf-8")
    assert _service_directives(unit) == {"PrivateTmp": "true"}
