"""Tests for :mod:`app.sonos_stream_favorites`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.sonos_stream_favorites import (
    SonosStreamFavorite,
    load_sonos_stream_favorites,
    resume_favorite,
)


def test_load_sonos_stream_favorites_from_secrets_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    secrets = tmp_path / "domesti-bot.config.json"
    secrets.write_text(
        json.dumps(
            {
                "domesti_secrets_key": "unused",
                "sonos_stream_favorites": [
                    {
                        "name": "Alvorada FM",
                        "uri": "https://example.com/alvorada.aac",
                    },
                    {
                        "name": "Jazz24",
                        "uri": "https://example.com/jazz24.aac",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DOMESTI_CONFIG_FILE", str(secrets))

    favorites = load_sonos_stream_favorites()
    assert len(favorites) == 2
    assert favorites[0].name == "Alvorada FM"
    assert favorites[1].name == "Jazz24"


def test_resume_favorite_returns_indexed_entry() -> None:
    favorites = (
        SonosStreamFavorite(name="First", uri="https://example.com/1"),
        SonosStreamFavorite(name="Second", uri="https://example.com/2"),
    )
    first = resume_favorite(favorites, favorite_index=0)
    assert first is not None
    assert first.name == "First"
    assert resume_favorite(favorites, favorite_index=2) is None
