"""Parse and default person name fields for the automation user roster."""

from __future__ import annotations


def default_display_name(first_name: str) -> str:
    """Return the default UI label for a user (first name)."""
    return first_name.strip()


def parse_person_name(full_name: str) -> tuple[str, str]:
    """Split a My Tracks display name into first and last name parts."""
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    first_name = parts[0]
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first_name, last_name
