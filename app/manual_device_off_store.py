"""Persist when a person last turned a device off through the UI or CLI.

Rules such as "turn the bedroom light on in the evening" use this record
(``device_not_manually_off_within``) to stay out of the way for a while after
someone switched the light off by hand. Only turn-offs issued by a person
through domesti-bot are recorded; rule-initiated and bulk "turn everything off"
actions are not, and changes made outside domesti-bot (a wall switch, the
vendor app) are invisible here by design.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import DeviceManualOff
from app.db.session import discovery_session, discovery_write
from app.device_display import format_device_display
from app.device_enums import DeviceFamilyId

_LOGGER = logging.getLogger(__name__)


def clear_manual_device_off(
    path: Path | None,
    family_id: DeviceFamilyId,
    device_id: str,
) -> None:
    """Forget the manual off for ``device_id`` (a person turned it back on)."""
    key = _normalized_device_id(device_id)
    if path is None or key is None:
        return

    def _write(session: Session) -> None:
        session.execute(
            delete(DeviceManualOff).where(
                DeviceManualOff.family_id == family_id.value,
                DeviceManualOff.device_id == key,
            )
        )

    discovery_write(path, _write)


def list_manual_device_off_at(path: Path | None) -> dict[tuple[DeviceFamilyId, str], float]:
    """Return ``(family, device_id) -> epoch`` of each device's last manual off."""
    if path is None:
        return {}
    with discovery_session(path) as session:
        rows = session.scalars(select(DeviceManualOff)).all()
        return {
            (DeviceFamilyId(row.family_id), row.device_id): row.last_manual_off_at
            for row in rows
            if row.family_id in _FAMILY_VALUES
        }


def note_manual_power_change(
    path: Path | None,
    family_id: DeviceFamilyId,
    device_id: str,
    *,
    is_on: bool,
) -> None:
    """Apply a person's power change: an off is recorded, an on clears the record.

    Best effort: the device has already switched by the time this runs, so a
    storage failure is logged and swallowed rather than reported as a failed
    action. The cost is only that a rule may not hold off after this change.
    """
    try:
        if is_on:
            clear_manual_device_off(path, family_id, device_id)
        else:
            record_manual_device_off(path, family_id, device_id)
    except Exception:
        _LOGGER.warning(
            "Could not store manual power change for %s (is_on=%s); rules will not hold off for it",
            format_device_display(device_id.strip(), None),
            is_on,
            exc_info=True,
        )


def record_manual_device_off(
    path: Path | None,
    family_id: DeviceFamilyId,
    device_id: str,
    *,
    now: float | None = None,
) -> None:
    """Record that a person just turned ``device_id`` off through the UI or CLI."""
    key = _normalized_device_id(device_id)
    if path is None or key is None:
        _LOGGER.debug(
            "Ignoring manual-off record family=%s path_set=%s device_id_blank=%s",
            family_id.value,
            path is not None,
            key is None,
        )
        return
    at = time.time() if now is None else now

    def _write(session: Session) -> None:
        row = session.get(DeviceManualOff, (family_id.value, key))
        if row is None:
            session.add(
                DeviceManualOff(
                    device_id=key,
                    family_id=family_id.value,
                    last_manual_off_at=at,
                    updated_at=at,
                )
            )
            return
        row.last_manual_off_at = at
        row.updated_at = at

    discovery_write(path, _write)


_FAMILY_VALUES = frozenset(member.value for member in DeviceFamilyId)


def _normalized_device_id(device_id: str) -> str | None:
    trimmed = device_id.strip()
    return trimmed or None
