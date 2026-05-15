"""Resolve stable compact-tile icon keys for the web UI.

Icons are chosen from the device **label** (Kasa alias / user display name),
optional **Kasa hardware model** (e.g. ``KL125`` bulb vs ``HS103`` plug), and
family. TP-Link app "rooms" are not available on the LAN path domesti-bot uses
today — users often encode the room in the alias (``Kitchen lamp``).
"""

from __future__ import annotations

import re

# Exact normalized labels from the mobile mock and common household names.
_LABEL_TO_ICON: dict[str, str] = {
    "basement": "outlet",
    "guest": "table",
    "hall": "pendant",
    "kitchen": "bulb",
    "office": "desk",
    "porch": "lantern",
}

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


def _is_lamp_like_label(normalized_label: str) -> bool:
    return any(
        token in normalized_label
        for token in ("lamp", "light", "bulb", "chandelier", "sconce")
    )


def _kasa_icon_from_label(label: str) -> str:
    normalized = re.sub(r"\s+", " ", label.strip().lower())
    if not normalized:
        return "bulb"
    for key, icon in _LABEL_TO_ICON.items():
        if key in normalized.split():
            if key == "basement" and _is_lamp_like_label(normalized):
                continue
            return icon
    if _is_lamp_like_label(normalized):
        return "bulb"
    if any(
        token in normalized
        for token in ("outlet", "plug", "socket", "receptacle")
    ):
        return "outlet"
    if "porch" in normalized or "lantern" in normalized:
        return "lantern"
    if "office" in normalized or "desk" in normalized:
        return "desk"
    if "hall" in normalized or "pendant" in normalized:
        return "pendant"
    if "guest" in normalized or "bedroom" in normalized:
        return "table"
    if "strip" in normalized:
        return "strip"
    if "fan" in normalized:
        return "fan"
    return "bulb"


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
        from_model = _kasa_icon_from_model(kasa_model)
        if from_model is not None:
            return from_model
        return _kasa_icon_from_label(label)
    return "bulb"
