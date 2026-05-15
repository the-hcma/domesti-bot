"""Tests for :mod:`app.ui_compact_icon`."""

from __future__ import annotations

from app.ui_compact_icon import resolve_compact_icon


def test_resolve_compact_icon_kasa_label_desk_object() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Desk",
            kind="switch",
        )
        == "desk"
    )


def test_resolve_compact_icon_kasa_basement_lamp_is_lamp() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Basement lamp",
            kind="switch",
        )
        == "lamp"
    )


def test_resolve_compact_icon_kasa_hall_light_is_light() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Hall light",
            kind="switch",
        )
        == "light"
    )


def test_resolve_compact_icon_kasa_highlight_is_not_light_icon() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Highlight strip",
            kind="switch",
        )
        == "strip"
    )


def test_resolve_compact_icon_kasa_basement_only_is_room() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Basement",
            kind="switch",
        )
        == "room_basement"
    )


def test_resolve_compact_icon_kasa_kitchen_only_is_room() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Kitchen",
            kind="switch",
        )
        == "room_kitchen"
    )


def test_resolve_compact_icon_kasa_kitchen_led_is_bulb() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Kitchen LED",
            kind="switch",
        )
        == "bulb"
    )


def test_resolve_compact_icon_kasa_model_kl_prefix() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Thing",
            kind="switch",
            kasa_model="KL125",
        )
        == "bulb"
    )


def test_resolve_compact_icon_kasa_model_hs_prefix() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Thing",
            kind="switch",
            kasa_model="HS103",
        )
        == "outlet"
    )


def test_resolve_compact_icon_sonos_and_tailwind() -> None:
    assert (
        resolve_compact_icon(
            family_id="sonos",
            label="Living",
            kind="speaker",
        )
        == "speaker"
    )
    assert (
        resolve_compact_icon(
            family_id="tailwind",
            label="Left",
            kind="door",
        )
        == "garage"
    )
