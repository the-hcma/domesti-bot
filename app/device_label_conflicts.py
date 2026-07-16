"""Detect display-name changes / collisions keyed by MAC address."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)

_PENDING: list[DeviceLabelConflict] = []


@dataclass(frozen=True, slots=True)
class DeviceLabelConflict:
    """One operator-visible display-name issue discovered at fetch/persist time."""

    backend: str
    mac_address: str
    kind: str
    previous_label: str
    current_label: str

    def format_message(self) -> str:
        if self.kind == "renamed":
            return (
                f"{self.backend}: MAC {self.mac_address} display name changed "
                f"{self.previous_label!r} → {self.current_label!r} "
                f"(update rules / UI labels if needed)"
            )
        return (
            f"{self.backend}: display name {self.current_label!r} is shared by "
            f"MAC {self.mac_address} and at least one other device "
            f"(rules keyed by name may be ambiguous)"
        )


def clear_device_label_conflicts() -> None:
    """Drop any conflicts recorded since the last drain (e.g. new bootstrap)."""

    _PENDING.clear()


def drain_device_label_conflicts() -> tuple[DeviceLabelConflict, ...]:
    """Return and clear recorded conflicts (stable order)."""

    out = tuple(_PENDING)
    _PENDING.clear()
    return out


def note_display_name_collision(
    *,
    backend: str,
    display_name: str,
    mac_addresses: list[str],
) -> None:
    """Record when the same display name is attached to multiple MACs."""

    label = display_name.strip()
    macs = sorted({m.strip().lower() for m in mac_addresses if m and m.strip()})
    if not label or len(macs) < 2:
        return
    for mac in macs:
        conflict = DeviceLabelConflict(
            backend=backend,
            mac_address=mac,
            kind="collision",
            previous_label=label,
            current_label=label,
        )
        _PENDING.append(conflict)
        _LOGGER.warning("%s", conflict.format_message())


def note_display_name_rename(
    *,
    backend: str,
    mac_address: str,
    previous_label: str | None,
    current_label: str | None,
) -> None:
    """Record when a known MAC reports a different vendor/display label."""

    mac = mac_address.strip().lower()
    prev = (previous_label or "").strip()
    cur = (current_label or "").strip()
    if not mac or not prev or not cur or prev.lower() == cur.lower():
        return
    conflict = DeviceLabelConflict(
        backend=backend,
        mac_address=mac,
        kind="renamed",
        previous_label=prev,
        current_label=cur,
    )
    _PENDING.append(conflict)
    _LOGGER.warning("%s", conflict.format_message())


def record_duplicate_preferred_labels(
    *,
    backend: str,
    devices: list[tuple[str, str]],
) -> None:
    """``devices`` is ``(mac_address, preferred_label)``; flag shared labels."""

    label_to_macs: dict[str, list[str]] = defaultdict(list)
    for mac, label in devices:
        mac_s = (mac or "").strip()
        label_s = (label or "").strip()
        if not mac_s or not label_s:
            continue
        # Skip labels that are just the MAC itself (no human display name).
        if label_s.lower() == mac_s.lower():
            continue
        label_to_macs[label_s].append(mac_s)
    for label, macs in sorted(label_to_macs.items(), key=lambda item: item[0].lower()):
        note_display_name_collision(backend=backend, display_name=label, mac_addresses=macs)
