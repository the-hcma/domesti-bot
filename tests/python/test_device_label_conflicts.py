"""Tests for :mod:`app.device_label_conflicts`."""

from __future__ import annotations

from app.device_label_conflicts import (
    clear_device_label_conflicts,
    drain_device_label_conflicts,
    note_display_name_rename,
    record_duplicate_preferred_labels,
)


def test_note_display_name_rename_records_change() -> None:
    clear_device_label_conflicts()
    note_display_name_rename(
        backend="kasa",
        mac_address="aa:bb:cc:dd:ee:ff",
        previous_label="Plug",
        current_label="Kitchen plug",
    )
    conflicts = drain_device_label_conflicts()
    assert len(conflicts) == 1
    assert "aa:bb:cc:dd:ee:ff" in conflicts[0].format_message()
    assert "Plug" in conflicts[0].format_message()
    assert "Kitchen plug" in conflicts[0].format_message()
    assert drain_device_label_conflicts() == ()


def test_note_display_name_rename_skips_identical() -> None:
    clear_device_label_conflicts()
    note_display_name_rename(
        backend="kasa",
        mac_address="aa:bb:cc:dd:ee:ff",
        previous_label="Plug",
        current_label="plug",
    )
    assert drain_device_label_conflicts() == ()


def test_record_duplicate_preferred_labels() -> None:
    clear_device_label_conflicts()
    record_duplicate_preferred_labels(
        backend="kasa",
        devices=[
            ("aa:bb:cc:dd:ee:01", "Plug"),
            ("aa:bb:cc:dd:ee:02", "Plug"),
            ("aa:bb:cc:dd:ee:03", "Other"),
        ],
    )
    conflicts = drain_device_label_conflicts()
    assert len(conflicts) == 2
    assert all("Plug" in c.format_message() for c in conflicts)
