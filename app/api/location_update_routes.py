"""Operator-facing location-update debug ingest."""

from __future__ import annotations

from http import HTTPStatus

from fastapi import APIRouter, Request, Response

from app.api.location_update_ingest import apply_location_update_webhook
from app.api.schemas import LocationUpdateWebhookIn
from app.api.settings_routes import discovery_cache_path_from_request

router = APIRouter(prefix="/v1/location_update", tags=["location_update"])


@router.put("/{participant_id}", status_code=HTTPStatus.NO_CONTENT)
async def put_location_update_participant(
    participant_id: str,
    body: LocationUpdateWebhookIn,
    request: Request,
) -> Response:
    """Manual location ingest for debugging (same semantics as the live webhook)."""
    cache_path = discovery_cache_path_from_request(request)
    if cache_path is None:
        return Response(status_code=HTTPStatus.CONFLICT)
    payload = body.model_copy(update={"participant_id": participant_id.strip()})
    apply_location_update_webhook(
        cache_path,
        payload,
        check_emergency_switch=True,
        persist_fix=True,
    )
    return Response(status_code=HTTPStatus.NO_CONTENT)
