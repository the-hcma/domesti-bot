"""Global Sonos radio stream favorites loaded from ``domesti-bot.config.json``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.db.secrets_key import _format_json_decode_error, secrets_json_path

_LOGGER = logging.getLogger(__name__)

_SECRETS_FAVORITES_KEY = "sonos_stream_favorites"


@dataclass(frozen=True, slots=True)
class SonosStreamFavorite:
    """One playable radio stream (human label + direct URI)."""

    name: str
    uri: str


def load_sonos_stream_favorites() -> tuple[SonosStreamFavorite, ...]:
    """Parse global ``sonos_stream_favorites`` from the gitignored config JSON file."""
    path = secrets_json_path()
    if not path.is_file():
        return ()
    try:
        text = path.read_text(encoding="utf-8")
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        detail = _format_json_decode_error(path, text, exc)
        raise ValueError(detail) from exc
    if not isinstance(raw, dict):
        raise ValueError(f"Expected JSON object in {path}, got {type(raw).__name__}")
    block = raw.get(_SECRETS_FAVORITES_KEY)
    if block is None:
        return ()
    if not isinstance(block, list):
        raise ValueError(f"Expected {_SECRETS_FAVORITES_KEY} to be a list, got {type(block).__name__}")
    parsed: list[SonosStreamFavorite] = []
    for index, entry in enumerate(block):
        favorite = _parse_favorite_entry(entry, index=index)
        if favorite is not None:
            parsed.append(favorite)
    return tuple(parsed)


def resume_favorite(
    favorites: tuple[SonosStreamFavorite, ...],
    *,
    favorite_index: int,
) -> SonosStreamFavorite | None:
    """Return the favorite at ``favorite_index``, or ``None`` when out of range."""
    if favorite_index < 0 or favorite_index >= len(favorites):
        return None
    return favorites[favorite_index]


def _parse_favorite_entry(raw: Any, *, index: int) -> SonosStreamFavorite | None:
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%d]: expected object, got %s",
            index,
            type(raw).__name__,
        )
        return None
    name = str(raw.get("name") or "").strip()
    uri = str(raw.get("uri") or "").strip()
    if not name or not uri:
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%d]: expected non-empty name and uri",
            index,
        )
        return None
    if not uri.startswith(("http://", "https://")):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%d]: expected http(s) uri, got %r",
            index,
            uri,
        )
        return None
    return SonosStreamFavorite(name=name, uri=uri)
