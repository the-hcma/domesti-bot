"""Per-zone Sonos radio stream favorites loaded from ``domesti-bot.config.json``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from app.db.secrets_key import secrets_json_path

_LOGGER = logging.getLogger(__name__)

_SECRETS_FAVORITES_KEY = "sonos_stream_favorites"


@dataclass(frozen=True, slots=True)
class SonosStreamFavorite:
    """One playable radio stream (human label + direct URI)."""

    name: str
    uri: str


def favorites_for_zone(
    config: dict[str, list[SonosStreamFavorite]],
    *,
    zone_uid: str,
    zone_name: str,
) -> tuple[SonosStreamFavorite, ...]:
    """Return favorites for a zone keyed by UID, display name, or ``*`` default."""
    uid = zone_uid.strip()
    name = zone_name.strip()
    for key in (uid, name):
        if key:
            hit = _lookup_favorites(config, key)
            if hit:
                return hit
    default = _lookup_favorites(config, "*")
    return default if default else ()


def load_sonos_stream_favorites_config() -> dict[str, list[SonosStreamFavorite]]:
    """Parse ``sonos_stream_favorites`` from the gitignored secrets JSON file."""
    path = secrets_json_path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _LOGGER.warning(
            "Skipping sonos_stream_favorites: expected valid JSON in %s, got %s",
            path,
            exc,
        )
        return {}
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites: expected JSON object in %s, got %s",
            path,
            type(raw).__name__,
        )
        return {}
    block = raw.get(_SECRETS_FAVORITES_KEY)
    if block is None:
        return {}
    if not isinstance(block, dict):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites: expected object, got %s",
            type(block).__name__,
        )
        return {}
    return _parse_favorites_block(block)


def resume_favorite_for_zone(
    config: dict[str, list[SonosStreamFavorite]],
    *,
    zone_uid: str,
    zone_name: str,
    favorite_index: int,
) -> SonosStreamFavorite | None:
    """Return the favorite at ``favorite_index``, or ``None`` when out of range."""
    favorites = favorites_for_zone(
        config,
        zone_uid=zone_uid,
        zone_name=zone_name,
    )
    if favorite_index < 0 or favorite_index >= len(favorites):
        return None
    return favorites[favorite_index]


def _lookup_favorites(
    config: dict[str, list[SonosStreamFavorite]],
    key: str,
) -> tuple[SonosStreamFavorite, ...]:
    needle = key.strip()
    if not needle:
        return ()
    direct = config.get(needle)
    if direct:
        return tuple(direct)
    lowered = needle.casefold()
    for config_key, entries in config.items():
        if config_key.strip().casefold() == lowered:
            return tuple(entries)
    return ()


def _parse_favorite_entry(raw: Any, *, zone_key: str, index: int) -> SonosStreamFavorite | None:
    if not isinstance(raw, dict):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%r][%d]: expected object, got %s",
            zone_key,
            index,
            type(raw).__name__,
        )
        return None
    name = str(raw.get("name") or "").strip()
    uri = str(raw.get("uri") or "").strip()
    if not name or not uri:
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%r][%d]: expected non-empty name and uri",
            zone_key,
            index,
        )
        return None
    if not uri.startswith(("http://", "https://")):
        _LOGGER.warning(
            "Skipping sonos_stream_favorites[%r][%d]: expected http(s) uri, got %r",
            zone_key,
            index,
            uri,
        )
        return None
    return SonosStreamFavorite(name=name, uri=uri)


def _parse_favorites_block(raw: dict[Any, Any]) -> dict[str, list[SonosStreamFavorite]]:
    out: dict[str, list[SonosStreamFavorite]] = {}
    for zone_key, entries_raw in raw.items():
        zone = str(zone_key).strip()
        if not zone:
            continue
        if not isinstance(entries_raw, list):
            _LOGGER.warning(
                "Skipping sonos_stream_favorites[%r]: expected list, got %s",
                zone_key,
                type(entries_raw).__name__,
            )
            continue
        parsed: list[SonosStreamFavorite] = []
        for index, entry in enumerate(entries_raw):
            favorite = _parse_favorite_entry(entry, zone_key=zone, index=index)
            if favorite is not None:
                parsed.append(favorite)
        if parsed:
            out[zone] = parsed
    return out
