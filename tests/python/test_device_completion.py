"""Hermetic tests for tab-completion ``Name (mac)`` tokens."""

from __future__ import annotations

from app.device_completion import (
    CompletionAlias,
    completion_alias_matches,
    device_completion_alias,
)
from app.device_display import format_device_display


def test_device_completion_alias_display_is_name_and_mac() -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    item = device_completion_alias(mac, "Porch lights")
    assert item.display == format_device_display(mac, "Porch lights")
    assert mac in item.matches
    assert "Porch lights" in item.matches
    assert item.display not in item.matches


def test_device_completion_alias_mac_only_when_label_missing() -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    item = device_completion_alias(mac, mac)
    assert item.display == mac
    assert item.matches == ()


def test_completion_alias_matches_mac_label_and_display_prefixes() -> None:
    mac = "aa:bb:cc:dd:ee:ff"
    item = device_completion_alias(mac, "Porch lights", "0")
    assert completion_alias_matches(item, "")
    assert completion_alias_matches(item, "porch")
    assert completion_alias_matches(item, "PORCH LIGHTS (AA:BB")
    assert completion_alias_matches(item, "aa:bb")
    assert completion_alias_matches(item, "0")
    assert not completion_alias_matches(item, "kitchen")


def test_completion_alias_namedtuple_shape() -> None:
    item = CompletionAlias(display="Lamp (aa:bb:cc:dd:ee:01)", matches=("aa:bb:cc:dd:ee:01", "Lamp"))
    assert item.display.startswith("Lamp")
    assert len(item.matches) == 2
