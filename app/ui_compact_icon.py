"""Resolve stable compact-tile icon keys for the web UI.

Icons are chosen from the device **label** (Kasa alias / user display name),
optional **Kasa hardware model** (e.g. ``KL125`` bulb vs ``HS103`` plug), and
family. TP-Link app "rooms" are not available on the LAN path domesti-bot uses
today — users often encode the room or object in the alias (``Kitchen lamp``).

Resolution order for Kasa switches:

1. **Object** tokens in the label (``lamp``, ``led``, ``plug``, ``fan``, …).
2. **Room** tokens when the label names a space (``kitchen``, ``bedroom``, …).
3. **Hardware model** prefix when the label is otherwise generic.
4. Default ``bulb``.
"""

from __future__ import annotations

import re

# Substrings in the normalized label → icon key (first match wins).
_OBJECT_SUBSTRINGS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("chandelier", "sconce", "lamp", "light", "bulb", "led"), "bulb"),
    (("strip",), "strip"),
    (("fan",), "fan"),
    (("outlet", "plug", "socket", "receptacle"), "outlet"),
    (("pendant",), "pendant"),
    (("lantern",), "lantern"),
    (("desk",), "desk"),
    (("nightstand", "night stand"), "table"),
)

# Whole-word room tokens (checked after object substrings).
_ROOM_WORDS_TO_ICON: dict[str, str] = {
    "attic": "room_attic",
    "basement": "room_basement",
    "bath": "room_bathroom",
    "bathroom": "room_bathroom",
    "bedroom": "room_bedroom",
    "deck": "room_porch",
    "dining": "room_dining",
    "entry": "room_hall",
    "family": "room_living",
    "foyer": "room_hall",
    "garage": "room_garage",
    "guest": "room_guest",
    "hall": "room_hall",
    "hallway": "room_hall",
    "kids": "room_bedroom",
    "kitchen": "room_kitchen",
    "laundry": "room_laundry",
    "living": "room_living",
    "master": "room_bedroom",
    "mudroom": "room_hall",
    "nursery": "room_bedroom",
    "office": "room_office",
    "pantry": "room_kitchen",
    "patio": "room_porch",
    "porch": "room_porch",
    "sunroom": "room_living",
}

# Multi-word room phrases (substring match).
_ROOM_PHRASES_TO_ICON: tuple[tuple[str, str], ...] = (
    ("living room", "room_living"),
    ("dining room", "room_dining"),
    ("family room", "room_living"),
    ("guest room", "room_guest"),
    ("laundry room", "room_laundry"),
    ("mud room", "room_hall"),
)

# Kasa / Tapo model families (longest prefix wins via sorted iteration).
_MODEL_PREFIX_TO_ICON: tuple[tuple[str, str], ...] = (
    ("EP40", "outlet"),
    ("EP25", "outlet"),
    ("HS100", "outlet"),
    ("HS103", "outlet"),
    ("HS105", "outlet"),
    ("HS110", "outlet"),
    ("KP115", "outlet"),
    ("KP125", "outlet"),
    ("KL", "bulb"),
    ("LB", "bulb"),
    ("TL", "bulb"),
    ("L5", "strip"),
    ("L6", "strip"),
    ("L7", "strip"),
    ("L9", "strip"),
)


def _icon_from_object_tokens(normalized: str) -> str | None:
    for tokens, icon in _OBJECT_SUBSTRINGS:
        if any(token in normalized for token in tokens):
            return icon
    return None


def _icon_from_room_tokens(normalized: str) -> str | None:
    for phrase, icon in _ROOM_PHRASES_TO_ICON:
        if phrase in normalized:
            return icon
    for word in normalized.split():
        mapped = _ROOM_WORDS_TO_ICON.get(word)
        if mapped is not None:
            return mapped
    return None


def _kasa_icon_from_model(model: str | None) -> str | None:
    if model is None:
        return None
    upper = model.strip().upper()
    if not upper:
        return None
    for prefix, icon in _MODEL_PREFIX_TO_ICON:
        if upper.startswith(prefix):
            return icon
    return None


def _normalize_label(label: str) -> str:
    return re.sub(r"\s+", " ", label.strip().lower())


def resolve_compact_icon(
    *,
    family_id: str,
    label: str,
    kind: str,
    kasa_model: str | None = None,
) -> str:
    """Return a stable icon key consumed by ``web/src/main.ts``."""
    if family_id == "sonos" or kind == "speaker":
        return "speaker"
    if family_id == "tailwind" or kind == "door":
        return "garage"
    if family_id == "kasa" or kind == "switch":
        normalized = _normalize_label(label)
        from_object = _icon_from_object_tokens(normalized)
        if from_object is not None:
            return from_object
        from_room = _icon_from_room_tokens(normalized)
        if from_room is not None:
            return from_room
        from_model = _kasa_icon_from_model(kasa_model)
        if from_model is not None:
            return from_model
        return "bulb"
    return "bulb"
