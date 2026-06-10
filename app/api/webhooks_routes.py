"""my-tracks location-update webhook routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.location_update_ingest import apply_location_update_webhook
from app.api.mytracks_relay_auth import verify_mytracks_relay_api_key
from app.api.schemas import LocationUpdateWebhookIn
from app.api.settings_routes import discovery_cache_path_from_request

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])

RelayAuth = Annotated[None, Depends(verify_mytracks_relay_api_key)]


@router.post("/location_update/test", status_code=HTTPStatus.NO_CONTENT)
async def post_location_update_test_webhook(
    body: LocationUpdateWebhookIn,
    request: Request,
    _auth: RelayAuth,
) -> Response:
    """Accept a synthetic test payload and discard it without mutating live location state."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return Response(status_code=HTTPStatus.UNAUTHORIZED)
    apply_location_update_webhook(
        cache_path,
        body,
        check_emergency_switch=False,
        persist_fix=False,
    )
    return Response(status_code=HTTPStatus.NO_CONTENT)


@router.post("/location_update", status_code=HTTPStatus.NO_CONTENT)
async def post_location_update_webhook(
    body: LocationUpdateWebhookIn,
    request: Request,
    _auth: RelayAuth,
) -> Response:
    """Accept a live GPS fix relayed from my-tracks."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return Response(status_code=HTTPStatus.UNAUTHORIZED)
    apply_location_update_webhook(
        cache_path,
        body,
        check_emergency_switch=True,
        persist_fix=True,
    )
    return Response(status_code=HTTPStatus.NO_CONTENT)
