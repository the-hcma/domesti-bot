"""Process-local device presence notices surfaced in the web UI (domesti-bot#867).

A device that stops responding raises a ``disappeared`` notice that asks the operator
whether to forget it or keep checking (the outage may be transient); a device that
answers again raises a ``reappeared`` notice. State is in memory: after a restart a
device that is still missing is announced again.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, replace

from app.device_enums import DevicePresenceDecision, DevicePresenceKind

# A "back online" notice is informational; drop it after a day if nobody dismisses it.
REAPPEARED_NOTICE_TTL_S: float = 24 * 60 * 60

_MAX_REAPPEARED_NOTICES = 50


@dataclass(frozen=True)
class DevicePresenceNotice:
    """One notice about a device that stopped or resumed responding."""

    decision: DevicePresenceDecision
    device_id: str
    family_id: str
    host: str | None
    kind: DevicePresenceKind
    label: str
    notice_id: str
    recorded_at: float


class DevicePresenceStore:
    """Tracks missing devices and the notices the web UI shows for them."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._missing: dict[tuple[str, str], DevicePresenceNotice] = {}
        self._reappeared: dict[str, DevicePresenceNotice] = {}

    def clear(self) -> None:
        with self._lock:
            self._missing.clear()
            self._reappeared.clear()

    def dismiss(self, notice_id: str) -> DevicePresenceNotice | None:
        """Remove a ``reappeared`` notice; ``None`` when there is no such notice."""

        with self._lock:
            return self._reappeared.pop(notice_id, None)

    def forget(self, family_id: str, device_id: str) -> None:
        """Stop tracking a device (the operator forgot it, or it is gone for good)."""

        with self._lock:
            self._missing.pop((family_id, device_id), None)

    def get(self, notice_id: str) -> DevicePresenceNotice | None:
        with self._lock:
            for notice in self._missing.values():
                if notice.notice_id == notice_id:
                    return notice
            return self._reappeared.get(notice_id)

    def keep_checking(self, notice_id: str) -> DevicePresenceNotice | None:
        """Record "keep checking": the notice is hidden until the device returns and goes missing again."""

        with self._lock:
            for key, notice in self._missing.items():
                if notice.notice_id == notice_id:
                    updated = replace(notice, decision=DevicePresenceDecision.KEEP_CHECKING)
                    self._missing[key] = updated
                    return updated
            return None

    def note_disappeared(
        self,
        *,
        device_id: str,
        family_id: str,
        host: str | None,
        label: str,
        now: float | None = None,
    ) -> DevicePresenceNotice | None:
        """Announce a device that stopped responding; ``None`` when it is already tracked."""

        key = (family_id, device_id)
        with self._lock:
            if key in self._missing:
                return None
            recorded_at = time.time() if now is None else now
            notice = DevicePresenceNotice(
                decision=DevicePresenceDecision.PENDING,
                device_id=device_id,
                family_id=family_id,
                host=host,
                kind=DevicePresenceKind.DISAPPEARED,
                label=label,
                notice_id=secrets.token_hex(8),
                recorded_at=recorded_at,
            )
            self._missing[key] = notice
            return notice

    def note_reappeared(
        self,
        *,
        device_id: str,
        family_id: str,
        host: str | None,
        label: str,
        now: float | None = None,
    ) -> DevicePresenceNotice | None:
        """Announce a device that answers again; ``None`` when it was never reported missing."""

        with self._lock:
            if self._missing.pop((family_id, device_id), None) is None:
                return None
            recorded_at = time.time() if now is None else now
            notice = DevicePresenceNotice(
                decision=DevicePresenceDecision.PENDING,
                device_id=device_id,
                family_id=family_id,
                host=host,
                kind=DevicePresenceKind.REAPPEARED,
                label=label,
                notice_id=secrets.token_hex(8),
                recorded_at=recorded_at,
            )
            self._reappeared = {
                nid: n for nid, n in self._reappeared.items() if (n.family_id, n.device_id) != (family_id, device_id)
            }
            self._reappeared[notice.notice_id] = notice
            while len(self._reappeared) > _MAX_REAPPEARED_NOTICES:
                oldest = min(self._reappeared.values(), key=lambda n: n.recorded_at)
                del self._reappeared[oldest.notice_id]
            return notice

    def notices(self, *, now: float | None = None) -> tuple[DevicePresenceNotice, ...]:
        """Notices to show now, oldest first: undecided disappearances and recent returns."""

        current = time.time() if now is None else now
        with self._lock:
            self._reappeared = {
                nid: n for nid, n in self._reappeared.items() if current - n.recorded_at <= REAPPEARED_NOTICE_TTL_S
            }
            shown = [n for n in self._missing.values() if n.decision == DevicePresenceDecision.PENDING]
            shown.extend(self._reappeared.values())
        return tuple(sorted(shown, key=lambda n: (n.recorded_at, n.notice_id)))


device_presence_store = DevicePresenceStore()
