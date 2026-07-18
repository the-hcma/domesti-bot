"""Human-visible device labels: ``preferred_label (mac)``."""

from __future__ import annotations


def format_device_display(device_id: str, display_name: str | None) -> str:
    """Return ``Name (mac)`` when ``display_name`` differs from ``device_id``, else the id alone."""
    trimmed_id = device_id.strip()
    if display_name is None:
        return trimmed_id
    trimmed_name = display_name.strip()
    if trimmed_name == "" or trimmed_name.casefold() == trimmed_id.casefold():
        return trimmed_id
    return f"{trimmed_name} ({trimmed_id})"
