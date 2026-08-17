"""Tab-completion tokens: ``preferred_label (mac)`` plus extra match prefixes."""

from __future__ import annotations

from typing import NamedTuple

from app.device_display import format_device_display


class CompletionAlias(NamedTuple):
    """One completer candidate: inserted ``display`` plus extra prefix matches."""

    display: str
    matches: tuple[str, ...]


def completion_alias_matches(item: CompletionAlias, prefix: str) -> bool:
    """Return whether ``prefix`` should complete to ``item.display`` (case-insensitive)."""

    needle = prefix.strip().lower()
    if not needle:
        return True
    if item.display.lower().startswith(needle):
        return True
    return any(m.lower().startswith(needle) for m in item.matches)


def device_completion_alias(
    identifier: str,
    preferred_label: str,
    *extra_matches: str,
) -> CompletionAlias:
    """Build a ``Name (mac)`` completion item; extra matches are MAC, label, door index, …"""

    display = format_device_display(identifier, preferred_label)
    seen: set[str] = {display.lower()}
    matches: list[str] = []
    for raw in (identifier, preferred_label, *extra_matches):
        token = (raw or "").strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        matches.append(token)
    return CompletionAlias(display=display, matches=tuple(matches))
