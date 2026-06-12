"""Parse and default person name fields for the automation user roster."""

from __future__ import annotations


def format_person_display_name(name: str) -> str:
    """Title-case each word for UI labels (``henrique`` → ``Henrique``)."""
    parts = name.strip().split()
    if not parts:
        return ""
    return " ".join(f"{part[0].upper()}{part[1:].lower()}" if part else "" for part in parts)


def default_display_name(first_name: str) -> str:
    """Return the default UI label for a user (first name)."""
    return format_person_display_name(first_name)


def parse_person_name(full_name: str) -> tuple[str, str]:
    """Split a My Tracks display name into first and last name parts."""
    parts = full_name.strip().split()
    if not parts:
        return "", ""
    first_name = parts[0]
    last_name = " ".join(parts[1:]) if len(parts) > 1 else ""
    return first_name, last_name
