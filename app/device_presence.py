"""Device presence notices surfaced in the web UI (domesti-bot#867, #874).

A device that stops responding raises a ``disappeared`` notice that asks the operator
whether to forget it or keep checking (the outage may be transient); a device that
answers again raises a ``reappeared`` notice.

Notices live in memory. The one answer worth keeping across a restart, "keep checking", is
stored in the discovery database once the store is attached to it (:meth:`DevicePresenceStore.attach`),
so a plug that is still missing after a restart stays quiet instead of asking again. The answer
expires after :data:`KEEP_CHECKING_TTL_S`, so a retired plug is eventually asked about again.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path

from app.device_display import format_device_display
from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence_decision_store import clear_keep_checking, list_keep_checking, record_keep_checking

# A "keep checking" answer is asked about again after a month, so a plug that was retired
# for good does not stay quietly "missing" forever.
KEEP_CHECKING_TTL_S: float = 30 * 24 * 60 * 60

# A "back online" notice is informational; drop it after a day if nobody dismisses it.
REAPPEARED_NOTICE_TTL_S: float = 24 * 60 * 60

_LOGGER = logging.getLogger(__name__)

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
        self._database_path: Path | None = None
        self._keep_checking: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()
        self._missing: dict[tuple[str, str], DevicePresenceNotice] = {}
        self._reappeared: dict[str, DevicePresenceNotice] = {}

    def attach(self, path: Path | None) -> None:
        """Keep "keep checking" answers in the discovery database at ``path`` (``None`` detaches)."""

        stored: dict[tuple[str, str], float] = {}
        if path is not None:
            try:
                stored = list_keep_checking(path)
            except Exception:
                _LOGGER.warning("Could not read stored keep-checking answers; asking again", exc_info=True)
        with self._lock:
            self._database_path = path
            self._keep_checking = stored

    def clear(self) -> None:
        with self._lock:
            self._database_path = None
            self._keep_checking.clear()
            self._missing.clear()
            self._reappeared.clear()

    @property
    def database_path(self) -> Path | None:
        return self._database_path

    def dismiss(self, notice_id: str) -> DevicePresenceNotice | None:
        """Remove a ``reappeared`` notice; ``None`` when there is no such notice."""

        with self._lock:
            return self._reappeared.pop(notice_id, None)

    def forget(self, family_id: str, device_id: str) -> None:
        """Stop tracking a device (the operator forgot it, or it is gone for good)."""

        key = (family_id, device_id)
        with self._lock:
            self._missing.pop(key, None)
            decided = self._keep_checking.pop(key, None)
        if decided is not None:
            self._clear_stored(key)

    def get(self, notice_id: str) -> DevicePresenceNotice | None:
        with self._lock:
            for notice in self._missing.values():
                if notice.notice_id == notice_id:
                    return notice
            return self._reappeared.get(notice_id)

    def keep_checking(self, notice_id: str, *, now: float | None = None) -> DevicePresenceNotice | None:
        """Record "keep checking": the notice is hidden until the device returns and goes missing again."""

        decided_at = time.time() if now is None else now
        with self._lock:
            for key, notice in self._missing.items():
                if notice.notice_id == notice_id:
                    updated = replace(notice, decision=DevicePresenceDecision.KEEP_CHECKING)
                    self._missing[key] = updated
                    self._keep_checking[key] = decided_at
                    break
            else:
                return None
        self._store(key, decided_at)
        return updated

    def missing_device_ids(self, family_id: str) -> set[str]:
        """Ids of the devices of ``family_id`` reported missing, or answered "keep checking" (any state)."""

        with self._lock:
            keys = set(self._missing) | set(self._keep_checking)
            return {device_id for family, device_id in keys if family == family_id}

    def note_disappeared(
        self,
        *,
        device_id: str,
        family_id: str,
        host: str | None,
        label: str,
        now: float | None = None,
    ) -> DevicePresenceNotice | None:
        """Announce a device that stopped responding; ``None`` when it is already tracked.

        A device the operator already answered "keep checking" for (before a restart too) comes
        back with that decision, so it stays hidden, unless the answer has expired.
        """

        key = (family_id, device_id)
        expired = False
        with self._lock:
            if key in self._missing:
                return None
            recorded_at = time.time() if now is None else now
            decided_at = self._keep_checking.get(key)
            if decided_at is not None and recorded_at - decided_at > KEEP_CHECKING_TTL_S:
                del self._keep_checking[key]
                decided_at = None
                expired = True
            notice = DevicePresenceNotice(
                decision=DevicePresenceDecision.PENDING if decided_at is None else DevicePresenceDecision.KEEP_CHECKING,
                device_id=device_id,
                family_id=family_id,
                host=host,
                kind=DevicePresenceKind.DISAPPEARED,
                label=label,
                notice_id=secrets.token_hex(8),
                recorded_at=recorded_at,
            )
            self._missing[key] = notice
        if expired:
            self._clear_stored(key)
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
        """Announce a device that answers again; ``None`` when it was never reported missing.

        A device answered "keep checking" before a restart counts as reported missing.
        """

        key = (family_id, device_id)
        with self._lock:
            was_missing = self._missing.pop(key, None)
            decided = self._keep_checking.pop(key, None)
            if was_missing is None and decided is None:
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
        if decided is not None:
            self._clear_stored(key)
        return notice

    def notices(self, *, now: float | None = None) -> tuple[DevicePresenceNotice, ...]:
        """Notices to show now, oldest first: undecided disappearances and recent returns.

        A "keep checking" answer older than :data:`KEEP_CHECKING_TTL_S` is asked about again.
        """

        current = time.time() if now is None else now
        lapsed: list[tuple[str, str]] = []
        with self._lock:
            self._reappeared = {
                nid: n for nid, n in self._reappeared.items() if current - n.recorded_at <= REAPPEARED_NOTICE_TTL_S
            }
            for key, notice in self._missing.items():
                if notice.decision != DevicePresenceDecision.KEEP_CHECKING:
                    continue
                decided_at = self._keep_checking.get(key)
                if decided_at is None or current - decided_at > KEEP_CHECKING_TTL_S:
                    self._missing[key] = replace(notice, decision=DevicePresenceDecision.PENDING)
                    self._keep_checking.pop(key, None)
                    lapsed.append(key)
            shown = [n for n in self._missing.values() if n.decision == DevicePresenceDecision.PENDING]
            shown.extend(self._reappeared.values())
        for key in lapsed:
            self._clear_stored(key)
        return tuple(sorted(shown, key=lambda n: (n.recorded_at, n.notice_id)))

    def _clear_stored(self, key: tuple[str, str]) -> None:
        """Best effort: a storage failure must not stop the in-memory state from changing."""

        try:
            clear_keep_checking(self._database_path, *key)
        except Exception:
            _LOGGER.warning(
                "Could not clear the stored keep-checking answer for %s",
                format_device_display(key[1], None),
                exc_info=True,
            )

    def _store(self, key: tuple[str, str], decided_at: float) -> None:
        """Best effort: the answer takes effect now even if it cannot be stored for the next start."""

        try:
            record_keep_checking(self._database_path, *key, now=decided_at)
        except Exception:
            _LOGGER.warning(
                "Could not store the keep-checking answer for %s; it will be asked again after a restart",
                format_device_display(key[1], None),
                exc_info=True,
            )


device_presence_store = DevicePresenceStore()
