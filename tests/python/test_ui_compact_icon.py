"""Tests for :mod:`app.ui_compact_icon`."""

from __future__ import annotations

from app.ui_compact_icon import resolve_compact_icon


def test_resolve_compact_icon_kasa_label_desk() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Desk",
            kind="switch",
        )
        == "desk"
    )


def test_resolve_compact_icon_kasa_basement_lamp_is_bulb() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Basement lamp",
            kind="switch",
        )
        == "bulb"
    )


def test_resolve_compact_icon_kasa_model_kl_prefix() -> None:
    assert (
        resolve_compact_icon(
            family_id="kasa",
            label="Plug",
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
