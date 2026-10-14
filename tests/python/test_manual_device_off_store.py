"""Tests for the persisted "turned off by a person" record."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.device_enums import DeviceFamilyId
from app.manual_device_off_store import (
    clear_manual_device_off,
    list_manual_device_off_at,
    note_manual_power_change,
    record_manual_device_off,
)

_MAC = "02:00:00:00:00:08"


def test_record_manual_device_off_is_listed_with_its_time(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_manual_device_off(path, DeviceFamilyId.KASA, _MAC, now=1000.0)
    assert list_manual_device_off_at(path) == {(DeviceFamilyId.KASA, _MAC): 1000.0}


def test_record_manual_device_off_restarts_window_on_second_off(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_manual_device_off(path, DeviceFamilyId.KASA, _MAC, now=1000.0)
    record_manual_device_off(path, DeviceFamilyId.KASA, _MAC, now=2500.0)
    assert list_manual_device_off_at(path) == {(DeviceFamilyId.KASA, _MAC): 2500.0}


def test_clear_manual_device_off_forgets_only_that_device(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    other = "02:00:00:00:00:09"
    record_manual_device_off(path, DeviceFamilyId.KASA, _MAC, now=1000.0)
    record_manual_device_off(path, DeviceFamilyId.KASA, other, now=1100.0)
    clear_manual_device_off(path, DeviceFamilyId.KASA, _MAC)
    assert list_manual_device_off_at(path) == {(DeviceFamilyId.KASA, other): 1100.0}


def test_note_manual_power_change_records_off_and_clears_on(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    note_manual_power_change(path, DeviceFamilyId.KASA, _MAC, is_on=False)
    assert (DeviceFamilyId.KASA, _MAC) in list_manual_device_off_at(path)
    note_manual_power_change(path, DeviceFamilyId.KASA, _MAC, is_on=True)
    assert list_manual_device_off_at(path) == {}


def test_manual_device_off_record_survives_a_new_session_on_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite"
    record_manual_device_off(path, DeviceFamilyId.KASA, _MAC, now=1000.0)
    reopened = Path(str(path))
    assert list_manual_device_off_at(reopened) == {(DeviceFamilyId.KASA, _MAC): 1000.0}


def test_store_functions_ignore_missing_path_and_blank_device_id(tmp_path: Path) -> None:
    record_manual_device_off(None, DeviceFamilyId.KASA, _MAC, now=1000.0)
    assert list_manual_device_off_at(None) == {}
    path = tmp_path / "cache.sqlite"
    record_manual_device_off(path, DeviceFamilyId.KASA, "  ", now=1000.0)
    assert list_manual_device_off_at(path) == {}


def test_note_manual_power_change_swallows_storage_failure_and_logs_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unusable_path = tmp_path  # a directory, so SQLite cannot open it as a database
    with caplog.at_level(logging.WARNING, logger="app.manual_device_off_store"):
        note_manual_power_change(unusable_path, DeviceFamilyId.KASA, _MAC, is_on=False)
    assert any(_MAC in record.getMessage() for record in caplog.records)
