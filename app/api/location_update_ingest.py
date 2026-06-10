"""Shared location-update webhook ingest helpers."""

from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path

from fastapi import HTTPException

from app.api.schemas import LocationUpdateWebhookIn
from app.mytracks_store import load_location_history_retention, load_mytracks_pair_status
from app.presence_store import ParticipantFixRecord, parse_iso_timestamp_to_epoch, upsert_participant_fix
from app.rules_store import participant_exists

_LOGGER = logging.getLogger(__name__)


def apply_location_update_webhook(
    cache_path: Path,
    body: LocationUpdateWebhookIn,
    *,
    check_emergency_switch: bool,
    persist_fix: bool,
) -> None:
    """Validate and optionally persist a location-update payload."""
    if check_emergency_switch and not _location_updates_accepted(cache_path):
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Location updates paused by operator",
            headers={"Retry-After": "60"},
        )
    participant_id = body.participant_id.strip()
    if not participant_exists(cache_path, participant_id):
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown participant_id {participant_id!r}",
        )
    if not persist_fix:
        _LOGGER.info(
            "[location] test webhook validated for %s (not persisted)",
            participant_id,
        )
        return
    try:
        received_at = parse_iso_timestamp_to_epoch(body.timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    retention = load_location_history_retention(cache_path)
    upsert_participant_fix(
        cache_path,
        ParticipantFixRecord(
            participant_id=participant_id,
            lat=body.lat,
            lon=body.lon,
            accuracy_m=body.accuracy_m,
            received_at=received_at,
            source=body.source or "my-tracks",
        ),
        retention=retention,
    )


def _location_updates_accepted(cache_path: Path) -> bool:
    status = load_mytracks_pair_status(cache_path)
    if status is None:
        return True
    return status.location_updates_accepted
