"""Tests for :mod:`app.device_presence` (no hardware)."""

from __future__ import annotations

from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence import REAPPEARED_NOTICE_TTL_S, DevicePresenceStore

_MAC = "5c:e9:31:60:c5:bd"


def _disappear(store: DevicePresenceStore, *, now: float = 1000.0) -> str:
    notice = store.note_disappeared(
        device_id=_MAC, family_id="kasa", host="192.168.86.167", label="Master bedroom fan", now=now
    )
    assert notice is not None
    return notice.notice_id


def test_note_disappeared_is_listed_as_pending_with_label_and_host() -> None:
    store = DevicePresenceStore()
    _disappear(store)
    (notice,) = store.notices(now=1001.0)
    assert notice.kind == DevicePresenceKind.DISAPPEARED
    assert notice.decision == DevicePresenceDecision.PENDING
    assert (notice.family_id, notice.device_id, notice.label, notice.host) == (
        "kasa",
        _MAC,
        "Master bedroom fan",
        "192.168.86.167",
    )


def test_note_disappeared_twice_announces_only_once() -> None:
    store = DevicePresenceStore()
    _disappear(store)
    assert store.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan") is None
    assert len(store.notices(now=1001.0)) == 1


def test_keep_checking_hides_notice_and_suppresses_repeat_announcement() -> None:
    store = DevicePresenceStore()
    notice_id = _disappear(store)
    updated = store.keep_checking(notice_id)
    assert updated is not None and updated.decision == DevicePresenceDecision.KEEP_CHECKING
    assert store.notices(now=1001.0) == ()
    assert store.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan") is None
    assert store.notices(now=1001.0) == ()


def test_note_reappeared_replaces_the_disappeared_notice() -> None:
    store = DevicePresenceStore()
    _disappear(store)
    back = store.note_reappeared(
        device_id=_MAC, family_id="kasa", host="192.168.86.167", label="Master bedroom fan", now=1100.0
    )
    assert back is not None
    (notice,) = store.notices(now=1101.0)
    assert notice.kind == DevicePresenceKind.REAPPEARED
    assert notice.notice_id == back.notice_id


def test_note_reappeared_after_keep_checking_still_announces_the_return() -> None:
    store = DevicePresenceStore()
    notice_id = _disappear(store)
    store.keep_checking(notice_id)
    assert store.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=1100.0) is not None


def test_note_reappeared_for_a_device_never_reported_missing_is_ignored() -> None:
    store = DevicePresenceStore()
    assert store.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan") is None
    assert store.notices() == ()


def test_dismiss_removes_only_reappeared_notices() -> None:
    store = DevicePresenceStore()
    missing_id = _disappear(store)
    assert store.dismiss(missing_id) is None
    back = store.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=1100.0)
    assert back is not None
    assert store.dismiss(back.notice_id) == back
    assert store.notices(now=1101.0) == ()


def test_reappeared_notice_expires_after_ttl() -> None:
    store = DevicePresenceStore()
    _disappear(store)
    store.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=1100.0)
    assert len(store.notices(now=1100.0 + REAPPEARED_NOTICE_TTL_S)) == 1
    assert store.notices(now=1100.0 + REAPPEARED_NOTICE_TTL_S + 1) == ()


def test_forget_stops_tracking_so_a_later_disappearance_is_announced_again() -> None:
    store = DevicePresenceStore()
    notice_id = _disappear(store)
    store.forget("kasa", _MAC)
    assert store.get(notice_id) is None
    assert store.notices(now=1001.0) == ()
    assert store.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan") is not None


def test_notices_are_ordered_oldest_first() -> None:
    store = DevicePresenceStore()
    store.note_disappeared(device_id="aa:bb:cc:dd:ee:02", family_id="kasa", host=None, label="Second", now=2000.0)
    store.note_disappeared(device_id="aa:bb:cc:dd:ee:01", family_id="kasa", host=None, label="First", now=1000.0)
    assert [n.label for n in store.notices(now=2001.0)] == ["First", "Second"]


def test_missing_device_ids_lists_only_missing_devices_of_that_family_whatever_the_decision() -> None:
    store = DevicePresenceStore()
    notice_id = _disappear(store)
    store.note_disappeared(device_id="aa:bb:cc:dd:ee:02", family_id="sonos", host=None, label="Kitchen")
    store.keep_checking(notice_id)
    assert store.missing_device_ids("kasa") == {_MAC}
    store.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan")
    assert store.missing_device_ids("kasa") == set()
