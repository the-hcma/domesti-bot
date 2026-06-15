"""Unit tests for :mod:`app.vizio_smartcast_client` helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.vizio_smartcast_client import (
    DEFAULT_VIZIO_PORT,
    VizioSmartCastClient,
    VizioSmartCastNotFoundError,
    VizioStateExtendedSnapshot,
    device_id_for,
    parse_host_spec,
    parse_state_extended,
    tv_is_active,
)


def test_parse_host_spec_defaults_port() -> None:
    assert parse_host_spec("192.168.1.10") == ("192.168.1.10", DEFAULT_VIZIO_PORT)


def test_parse_host_spec_parses_explicit_port() -> None:
    assert parse_host_spec("192.168.1.10:7345") == ("192.168.1.10", 7345)


def test_parse_host_spec_rejects_empty() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        parse_host_spec("   ")


def test_device_id_for_omits_default_port() -> None:
    assert device_id_for("192.168.1.10", DEFAULT_VIZIO_PORT) == "192.168.1.10"


def test_device_id_for_includes_non_default_port() -> None:
    assert device_id_for("192.168.1.10", 7346) == "192.168.1.10:7346"


def test_parse_state_extended_reads_power_and_media() -> None:
    snapshot = parse_state_extended(
        {
            "POWER_STATUS": {"VALUE": 0},
            "POWER_MODE": {"VALUE": "Active Off"},
            "CURRENT_INPUT": {"NAME": "SMARTCAST"},
            "APP_CURRENT": {"APP_ID": "2", "NAME_SPACE": 4},
            "MEDIA_STATE": "MediaState::Playing",
        }
    )
    assert snapshot.power_on is False
    assert snapshot.current_input == "SMARTCAST"
    assert snapshot.media_state == "MediaState::Playing"
    assert snapshot.has_current_app is True


def test_tv_is_active_when_panel_off_but_cast_playing() -> None:
    assert tv_is_active(
        power_on=False,
        current_input="SMARTCAST",
        media_state="MediaState::Playing",
        has_current_app=True,
    )


def test_tv_is_active_when_panel_on() -> None:
    assert tv_is_active(power_on=True, media_state="MediaState::Stopped")


def test_tv_is_active_when_panel_off_and_idle() -> None:
    assert not tv_is_active(
        power_on=False,
        current_input="SMARTCAST",
        media_state="MediaState::Stopped",
        has_current_app=True,
    )


@pytest.mark.asyncio
async def test_fetch_tv_active_state_falls_back_to_power_mode() -> None:
    client = VizioSmartCastClient("192.168.1.10", auth_token="token")
    with (
        patch.object(
            client,
            "fetch_state_extended",
            new_callable=AsyncMock,
            side_effect=VizioSmartCastNotFoundError("missing"),
        ),
        patch.object(
            client,
            "get_power_on",
            new_callable=AsyncMock,
            return_value=True,
        ) as get_power_on,
    ):
        assert await client.fetch_tv_active_state() is True
    get_power_on.assert_awaited_once()


@pytest.mark.asyncio
async def test_fetch_tv_active_state_uses_state_extended_when_available() -> None:
    client = VizioSmartCastClient("192.168.1.10", auth_token="token")
    snapshot = VizioStateExtendedSnapshot(
        power_on=False,
        power_mode="Active Off",
        current_input="SMARTCAST",
        media_state="MediaState::Playing",
        has_current_app=True,
    )
    with patch.object(
        client,
        "fetch_state_extended",
        new_callable=AsyncMock,
        return_value=snapshot,
    ):
        assert await client.fetch_tv_active_state() is True
