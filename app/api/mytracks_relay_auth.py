"""Relay API key verification for my-tracks webhook ingest."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import Header, HTTPException, Request

from app.api.api_key_compare import api_keys_match
from app.api.settings_routes import discovery_cache_path_from_request
from app.db.secrets import load_mytracks_relay_api_key_from_db
from app.mytracks_relay_keys import load_state, uses_split_keys, verify_inbound


async def verify_mytracks_relay_api_key(
    request: Request,
    x_domesti_api_key: Annotated[str | None, Header(alias="X-Domesti-Api-Key")] = None,
) -> None:
    """Validate ``X-Domesti-Api-Key`` against the paired relay credentials in SQLite.

    Protocol 1 compares the single shared key. Protocol 2 compares keyed verifiers of the active key, of a
    staged key while a pairing is in progress, and of the previous key during its short grace. While a protocol 2
    pairing is being staged over a protocol 1 one, the old shared key keeps working too.
    """
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="My Tracks relay not configured",
        )
    provided = (x_domesti_api_key or "").strip()
    split = uses_split_keys(cache_path)
    legacy_ok = False
    legacy_configured = False
    if load_state(cache_path).protocol_version < 2:
        relay_key = load_mytracks_relay_api_key_from_db(cache_path)
        legacy_configured = relay_key is not None and relay_key.strip() != ""
        legacy_ok = legacy_configured and relay_key is not None and api_keys_match(provided, relay_key)
    split_ok = split and verify_inbound(cache_path, provided)
    if not split and not legacy_configured:
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="My Tracks relay not configured",
        )
    if not (legacy_ok or split_ok):
        raise HTTPException(
            status_code=HTTPStatus.UNAUTHORIZED,
            detail="Invalid or missing X-Domesti-Api-Key",
        )
