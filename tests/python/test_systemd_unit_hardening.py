"""Baseline hardening of the shipped systemd user unit (domesti-bot#879)."""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_USER_UNIT = _REPO_ROOT / "etc" / "systemd" / "domesti-bot.service"
# What the process needs: sockets for HTTP, mDNS/SSDP and UDP discovery (AF_INET*), the C library's
# interface listing (AF_NETLINK) and local IPC (AF_UNIX). Raw packet sockets are not used.
_REQUIRED_ADDRESS_FAMILIES = frozenset({"AF_INET", "AF_INET6", "AF_NETLINK", "AF_UNIX"})


def _assignments(path: Path) -> dict[str, list[str]]:
    """Return every ``[Service]`` assignment of a unit file, in order, per key.

    A trailing backslash joins the next line, as systemd does. Repeats are kept because systemd
    accumulates some keys (``RestrictAddressFamilies``) instead of letting the last one win.
    """

    joined: list[str] = []
    pending = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if pending and (not stripped or stripped.startswith(("#", ";"))):
            # systemd skips comment and blank lines inside a continued value and keeps joining.
            continue
        line = (pending + stripped) if pending else stripped
        if line.endswith("\\"):
            pending = line[:-1].rstrip() + " "
            continue
        pending = ""
        joined.append(line)
    assignments: dict[str, list[str]] = {}
    in_service = False
    for line in joined:
        if not line or line.startswith(("#", ";")):
            continue
        header = re.fullmatch(r"\[(\w+)\]", line)
        if header is not None:
            in_service = header.group(1) == "Service"
            continue
        if in_service and "=" in line:
            key, _, value = line.partition("=")
            assignments.setdefault(key.strip(), []).append(value.strip())
    return assignments


def _single(path: Path, key: str) -> str:
    values = _assignments(path).get(key, [])
    assert len(values) == 1, f"Expected exactly one {key}= assignment, got {values}"
    return values[0]


def test_user_unit_forbids_gaining_privileges() -> None:
    assert _single(_USER_UNIT, "NoNewPrivileges") == "true"


def test_user_unit_has_a_private_tmp_and_a_read_only_system() -> None:
    assert _single(_USER_UNIT, "PrivateTmp") == "true"
    assert _single(_USER_UNIT, "ProtectSystem") == "full"


def test_user_unit_restricts_address_families_to_what_the_service_uses() -> None:
    families = set(_single(_USER_UNIT, "RestrictAddressFamilies").split())
    assert families == _REQUIRED_ADDRESS_FAMILIES, (
        f"Expected {sorted(_REQUIRED_ADDRESS_FAMILIES)}, got {sorted(families)}"
    )


def test_user_unit_keeps_the_home_directory_writable_and_the_syscalls_unfiltered() -> None:
    # The service writes its log, discovery cache and rules under the home directory and its checkout,
    # and a syscall filter can break a service at runtime.
    assignments = _assignments(_USER_UNIT)
    assert "ProtectHome" not in assignments
    assert "SystemCallFilter" not in assignments
    assert assignments.get("ProtectSystem") != ["strict"]


def test_the_parser_ignores_other_sections_and_comments(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Unit]\nNoNewPrivileges=false\n[Service]\n# PrivateTmp=false\nPrivateTmp=true\n", encoding="utf-8")
    assert _assignments(unit) == {"PrivateTmp": ["true"]}


def test_the_parser_joins_continued_lines_and_keeps_repeated_keys(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text(
        "[Service]\nRestrictAddressFamilies=AF_INET \\\n  AF_UNIX\nRestrictAddressFamilies=AF_PACKET\n",
        encoding="utf-8",
    )
    assert _assignments(unit) == {"RestrictAddressFamilies": ["AF_INET AF_UNIX", "AF_PACKET"]}


def test_a_repeated_pinned_directive_is_rejected(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text("[Service]\nRestrictAddressFamilies=AF_INET\nRestrictAddressFamilies=AF_PACKET\n", encoding="utf-8")
    try:
        _single(unit, "RestrictAddressFamilies")
    except AssertionError as exc:
        assert "exactly one" in str(exc)
    else:
        raise AssertionError("Expected a repeated directive to be rejected")


def test_the_parser_skips_comment_lines_inside_a_continued_value(tmp_path: Path) -> None:
    unit = tmp_path / "x.service"
    unit.write_text(
        "[Service]\nRestrictAddressFamilies=AF_INET \\\n# a comment between the lines\n  AF_UNIX\n",
        encoding="utf-8",
    )
    assert _assignments(unit) == {"RestrictAddressFamilies": ["AF_INET AF_UNIX"]}
