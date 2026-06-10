"""Relay API key verification for my-tracks webhook ingest."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import load_mytracks_relay_api_key_from_db


async def verify_mytracks_relay_api_key(
    request: Request,
    x_domesti_api_key: Annotated[str | None, Header(alias="X-Domesti-Api-Key")] = None,
) -> None:
    """Validate ``X-Domesti-Api-Key`` against the paired relay secret in SQLite."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="My Tracks relay not configured",
        )
    relay_key = load_mytracks_relay_api_key_from_db(cache_path)
    if relay_key is None or relay_key.strip() == "":
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="My Tracks relay not configured",
        )
    provided = (x_domesti_api_key or "").strip()
    if provided != relay_key:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="Invalid or missing X-Domesti-Api-Key",
        )
