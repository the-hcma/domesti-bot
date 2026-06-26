"""Shared location-update webhook ingest helpers."""

from __future__ import annotations

import logging
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

from fastapi import HTTPException

from app.api.schemas import LocationUpdateWebhookIn
from app.mytracks_store import load_location_history_retention, load_mytracks_pair_status
from app.presence_store import (
    UserLocationRecord,
    parse_iso_timestamp_to_epoch,
    upsert_user_location,
)
from app.rules_store import user_exists

_LOGGER = logging.getLogger("location")


def apply_location_update_webhook(
    cache_path: Path,
    body: LocationUpdateWebhookIn,
    *,
    after_persist: Callable[[str], None] | None = None,
    check_emergency_switch: bool,
    persist_location: bool,
) -> None:
    """Validate and optionally persist a location-update payload."""
    if check_emergency_switch and not _location_updates_accepted(cache_path):
        raise HTTPException(
            status_code=HTTPStatus.SERVICE_UNAVAILABLE,
            detail="Location updates paused by operator",
            headers={"Retry-After": "60"},
        )
    user_id = body.user_id.strip()
    if not persist_location:
        _LOGGER.info("test webhook accepted for %s (discarded)", user_id)
        return
    if not user_exists(cache_path, user_id):
        raise HTTPException(
            status_code=HTTPStatus.NOT_FOUND,
            detail=f"Unknown user_id {user_id!r}",
        )
    try:
        received_at = parse_iso_timestamp_to_epoch(body.timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    retention = load_location_history_retention(cache_path)
    stored = upsert_user_location(
        cache_path,
        user_location_record_from_webhook(body, user_id=user_id, received_at=received_at),
        retention=retention,
    )
    if stored and after_persist is not None:
        after_persist(user_id)


def user_location_record_from_webhook(
    body: LocationUpdateWebhookIn,
    *,
    user_id: str,
    received_at: float,
) -> UserLocationRecord:
    """Build a ``UserLocationRecord`` from a validated webhook body."""
    return UserLocationRecord(
        user_id=user_id,
        lat=body.lat,
        lon=body.lon,
        accuracy_m=body.accuracy_m,
        battery_level=body.battery_level,
        connection_type=body.connection_type,
        fix_source=body.fix_source,
        received_at=received_at,
        source=body.source or "my-tracks",
        trigger=body.trigger,
        wifi_bssid=body.wifi_bssid,
        wifi_ssid=body.wifi_ssid,
    )


def _location_updates_accepted(cache_path: Path) -> bool:
    status = load_mytracks_pair_status(cache_path)
    if status is None:
        return True
    return status.location_updates_accepted
