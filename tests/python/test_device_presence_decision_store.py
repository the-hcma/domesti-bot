"""Tests for the persisted "keep checking" decisions (domesti-bot#874)."""

from __future__ import annotations

from pathlib import Path

from app.device_presence_decision_store import (
    clear_keep_checking,
    list_keep_checking,
    record_keep_checking,
)

_MAC = "5c:e9:31:60:c5:bd"
_OTHER = "aa:bb:cc:dd:ee:02"


def test_record_keep_checking_is_listed_with_its_time(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_keep_checking(path, "kasa", _MAC, now=1000.0)
    assert list_keep_checking(path) == {("kasa", _MAC): 1000.0}


def test_record_keep_checking_again_restarts_the_clock(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_keep_checking(path, "kasa", _MAC, now=1000.0)
    record_keep_checking(path, "kasa", _MAC, now=2500.0)
    assert list_keep_checking(path) == {("kasa", _MAC): 2500.0}


def test_clear_keep_checking_forgets_only_that_device(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_keep_checking(path, "kasa", _MAC, now=1000.0)
    record_keep_checking(path, "kasa", _OTHER, now=1100.0)
    clear_keep_checking(path, "kasa", _MAC)
    assert list_keep_checking(path) == {("kasa", _OTHER): 1100.0}


def test_decisions_are_keyed_by_family_as_well_as_device(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_keep_checking(path, "kasa", _MAC, now=1000.0)
    record_keep_checking(path, "sonos", _MAC, now=1200.0)
    clear_keep_checking(path, "sonos", _MAC)
    assert list_keep_checking(path) == {("kasa", _MAC): 1000.0}


def test_without_a_database_path_nothing_is_stored_or_listed() -> None:
    record_keep_checking(None, "kasa", _MAC, now=1000.0)
    clear_keep_checking(None, "kasa", _MAC)
    assert list_keep_checking(None) == {}
