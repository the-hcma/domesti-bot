"""Canonical rule ``device_id`` helpers (MAC / Tailwind composite).

Automation rules store authoritative device targets as normalized MACs (or
``{hub_mac}:{door_id}`` for Tailwind). Display names remain resolvable at
runtime for migration compat but are non-authoritative — validation surfaces a
warning when a rule still uses one.
"""

from __future__ import annotations

from app.device_enums import DeviceFamilyId
from app.device_mac import is_normalized_mac, try_normalize_mac

# Public message constants (asserted by tests — do not hard-code prose there).
RULE_DEVICE_ID_DISPLAY_NAME_WARNING = (
    'Device id "{device_id}" looks like a display name; store the MAC address '
    "(or Tailwind {{hub_mac}}:{{door_id}}) as the authoritative device_id."
)


def is_canonical_rule_device_id(family_id: DeviceFamilyId, device_id: str) -> bool:
    """Return True when ``device_id`` is the authoritative on-disk form for ``family_id``."""
    trimmed = device_id.strip()
    if not trimmed:
        return False
    if family_id == DeviceFamilyId.TAILWIND:
        return is_tailwind_composite_device_id(trimmed)
    return is_normalized_mac(trimmed)


def is_tailwind_composite_device_id(device_id: str) -> bool:
    """True when ``device_id`` is ``{normalized_hub_mac}:{door_id}`` with a non-empty door id."""
    trimmed = device_id.strip()
    if ":" not in trimmed:
        return False
    # MAC has five colons (six octets); composite adds one more before door_id.
    parts = trimmed.split(":")
    if len(parts) < 7:
        return False
    mac = ":".join(parts[:6])
    door_id = ":".join(parts[6:]).strip()
    if not door_id:
        return False
    return is_normalized_mac(mac)


def non_canonical_device_id_detail(device_id: str) -> str:
    """Operator-facing warning detail for a display-name style rule device_id."""
    return RULE_DEVICE_ID_DISPLAY_NAME_WARNING.format(device_id=device_id.strip())


def try_parse_tailwind_composite_device_id(device_id: str) -> tuple[str, str] | None:
    """Return ``(hub_mac, door_id)`` when ``device_id`` is a Tailwind composite id."""
    trimmed = device_id.strip()
    if not is_tailwind_composite_device_id(trimmed):
        return None
    parts = trimmed.split(":")
    mac = try_normalize_mac(":".join(parts[:6]))
    if mac is None:
        return None
    door_id = ":".join(parts[6:]).strip()
    return mac, door_id
