"""Compact icon resolution for the Vizio family."""

from __future__ import annotations

from app.ui_compact_icon import resolve_compact_icon


def test_resolve_compact_icon_returns_tv_for_vizio_family() -> None:
    assert (
        resolve_compact_icon(
            family_id="vizio",
            label="Kitchen TV",
            kind="switch",
        )
        == "tv"
    )
