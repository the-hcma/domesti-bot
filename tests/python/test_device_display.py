"""Hermetic tests for ``format_device_display``."""

from __future__ import annotations

from app.device_display import format_device_display


def test_format_device_display_includes_name_and_mac() -> None:
    assert format_device_display("dc:62:79:6c:86:77", "HDHomeRun tuner") == "HDHomeRun tuner (dc:62:79:6c:86:77)"


def test_format_device_display_omits_redundant_name() -> None:
    assert format_device_display("aa:bb:cc:dd:ee:ff", "aa:bb:cc:dd:ee:ff") == "aa:bb:cc:dd:ee:ff"
    assert format_device_display("aa:bb:cc:dd:ee:ff", None) == "aa:bb:cc:dd:ee:ff"
    assert format_device_display("aa:bb:cc:dd:ee:ff", "  ") == "aa:bb:cc:dd:ee:ff"
