"""Tests for :mod:`app.device_presence` (no hardware)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.device_enums import DevicePresenceDecision, DevicePresenceKind
from app.device_presence import KEEP_CHECKING_TTL_S, REAPPEARED_NOTICE_TTL_S, DevicePresenceStore
from app.device_presence_decision_store import list_keep_checking

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


def _attached(path: Path) -> DevicePresenceStore:
    store = DevicePresenceStore()
    store.attach(path)
    return store


def test_keep_checking_survives_a_restart_and_keeps_the_notice_hidden(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    before = _attached(path)
    notice_id = _disappear(before)
    before.keep_checking(notice_id, now=1500.0)

    after = _attached(path)  # a restarted process: same file, empty memory
    notice = after.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=2000.0)
    assert notice is not None and notice.decision == DevicePresenceDecision.KEEP_CHECKING
    assert after.notices(now=2001.0) == ()


def test_a_device_that_returns_after_a_restart_announces_the_return_and_clears_the_decision(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    before = _attached(path)
    before.keep_checking(_disappear(before), now=1500.0)

    after = _attached(path)
    back = after.note_reappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=2000.0)
    assert back is not None
    assert list_keep_checking(path) == {}
    # The next outage is a new question, not a silently remembered answer.
    later = after.note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan", now=3000.0)
    assert later is not None and later.decision == DevicePresenceDecision.PENDING


def test_forget_removes_the_persisted_decision(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    store = _attached(path)
    store.keep_checking(_disappear(store), now=1500.0)
    store.forget("kasa", _MAC)
    assert list_keep_checking(path) == {}
    assert _attached(path).note_disappeared(device_id=_MAC, family_id="kasa", host=None, label="Fan") is not None


def test_an_old_keep_checking_decision_is_asked_again(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    store = _attached(path)
    store.keep_checking(_disappear(store, now=1000.0), now=1000.0)
    assert store.notices(now=1000.0 + KEEP_CHECKING_TTL_S - 1) == ()
    (asked_again,) = store.notices(now=1000.0 + KEEP_CHECKING_TTL_S + 1)
    assert asked_again.decision == DevicePresenceDecision.PENDING
    assert list_keep_checking(path) == {}


def test_a_decision_older_than_the_limit_is_not_restored_after_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    before = _attached(path)
    before.keep_checking(_disappear(before, now=1000.0), now=1000.0)
    after = _attached(path)
    notice = after.note_disappeared(
        device_id=_MAC, family_id="kasa", host=None, label="Fan", now=1000.0 + KEEP_CHECKING_TTL_S + 1
    )
    assert notice is not None and notice.decision == DevicePresenceDecision.PENDING


def test_a_decision_for_a_device_that_left_the_roster_is_listed_so_it_can_be_cleaned_up(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    before = _attached(path)
    before.keep_checking(_disappear(before), now=1500.0)
    after = _attached(path)
    assert after.missing_device_ids("kasa") == {_MAC}


def test_clear_detaches_the_database(tmp_path: Path) -> None:
    store = _attached(tmp_path / "cache.sqlite")
    store.clear()
    assert store.database_path is None


def test_a_storage_failure_does_not_stop_the_decision_taking_effect(tmp_path: Path) -> None:
    store = DevicePresenceStore()
    store.attach(tmp_path / "missing-dir" / "nested" / "cache.sqlite")
    with patch("app.device_presence.record_keep_checking", side_effect=OSError("disk full")):
        updated = store.keep_checking(_disappear(store), now=1500.0)
    assert updated is not None and updated.decision == DevicePresenceDecision.KEEP_CHECKING
    assert store.notices(now=1501.0) == ()
