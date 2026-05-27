"""Tests for :mod:`app.sonos_stream_favorites`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.sonos_stream_favorites import (
    SonosStreamFavorite,
    favorites_for_zone,
    load_sonos_stream_favorites_config,
    resume_favorite_for_zone,
)


def test_load_sonos_stream_favorites_from_secrets_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "domesti-secrets.json"
    secrets.write_text(
        json.dumps(
            {
                "domesti_secrets_key": "unused",
                "sonos_stream_favorites": {
                    "Kitchen": [
                        {
                            "name": "Alvorada FM",
                            "uri": "https://example.com/alvorada.aac",
                        }
                    ],
                    "*": [
                        {
                            "name": "Fallback",
                            "uri": "https://example.com/fallback.mp3",
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_SECRETS_FILE", str(secrets))

    config = load_sonos_stream_favorites_config()
    assert list(config.keys()) == ["Kitchen", "*"]
    assert config["Kitchen"][0].name == "Alvorada FM"


def test_favorites_for_zone_matches_uid_then_name_then_default() -> None:
    config = {
        "RINCON_AAA": [
            SonosStreamFavorite(name="By UID", uri="https://example.com/uid")
        ],
        "kitchen": [
            SonosStreamFavorite(name="By Name", uri="https://example.com/name")
        ],
        "*": [
            SonosStreamFavorite(name="Default", uri="https://example.com/default")
        ],
    }
    by_uid = favorites_for_zone(
        config,
        zone_uid="RINCON_AAA",
        zone_name="Kitchen",
    )
    assert by_uid[0].name == "By UID"

    by_name = favorites_for_zone(
        config,
        zone_uid="RINCON_BBB",
        zone_name="Kitchen",
    )
    assert by_name[0].name == "By Name"

    by_default = favorites_for_zone(
        config,
        zone_uid="RINCON_CCC",
        zone_name="Office",
    )
    assert by_default[0].name == "Default"


def test_resume_favorite_for_zone_returns_indexed_entry() -> None:
    config = {
        "Kitchen": [
            SonosStreamFavorite(name="First", uri="https://example.com/1"),
            SonosStreamFavorite(name="Second", uri="https://example.com/2"),
        ]
    }
    first = resume_favorite_for_zone(
        config,
        zone_uid="RINCON_A",
        zone_name="Kitchen",
        favorite_index=0,
    )
    assert first is not None
    assert first.name == "First"
    assert resume_favorite_for_zone(
        config,
        zone_uid="RINCON_A",
        zone_name="Kitchen",
        favorite_index=2,
    ) is None
